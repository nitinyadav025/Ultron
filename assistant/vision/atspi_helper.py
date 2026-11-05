#!/usr/bin/python3
"""Persistent AT-SPI2 tree walker for Ultron, run under the system Python.

Fedora 44 ships ``python3-gobject`` and ``at-spi2-core`` in ``/usr/bin/python3``; the
project venv has no ``gi``. This helper therefore imports only ``gi`` and the standard
library, speaks one JSON object per line on stdin/stdout, and never imports the assistant
package.

Commands (one JSON object per line):

``{"cmd": "snapshot", "max_elements": 40, "max_nodes": 2500, "max_depth": 24}``
    Walk the focused window first, then the other visible windows. Return the window list
    plus interactive elements with screen-absolute bounding boxes.

``{"cmd": "click", "ref": "@e5"}``
    Invoke the element's AT-SPI click/press action (semantic Tier 1).

``{"cmd": "focus", "ref": "@e5"}``
    Grab keyboard focus on the element.

``{"cmd": "set_text", "ref": "@e5", "text": "..."}``
    Replace an editable element's contents through the accessibility API.

Every response is one compact JSON line: ``{"ok": true, ...}`` or
``{"ok": false, "error": "..."}``. Nothing is written to disk.
"""

from __future__ import annotations

import json
import sys
import warnings

# GTK4 deprecates get_action_name but still implements it; the warning would pollute the
# helper's stderr on every snapshot.
warnings.filterwarnings("ignore", category=DeprecationWarning)


def _load_atspi():
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    return Atspi


# The roles Ultron is allowed to see and click. GTK4/Libadwaita mostly reports "button",
# "toggle button", "entry" and "page tab"; Firefox reports the web roles.
INTERACTIVE_ROLES = frozenset(
    {
        "push button",
        "button",
        "toggle button",
        "switch",
        "link",
        "entry",
        "text",
        "password text",
        "menu item",
        "page tab",
        "check box",
        "radio button",
        "combo box",
        "option",
        "slider",
        "spin button",
        "heading",
        "document web",
        "document frame",
    }
)
EDITABLE_ROLES = frozenset({"entry", "text", "password text", "document web"})
WINDOW_ROLES = frozenset({"frame", "window", "dialog"})
# The shell and its helpers expose their own "Main stage" frame. It is never the user's
# focused application and would otherwise flood the element list with dock entries.
SYSTEM_APPS = frozenset(
    {
        "gnome-shell",
        "ibus-extension-gtk3",
        "evolution-alarm-notify",
        "gsconnect",
        "gjs-console",
        "xdg-desktop-portal-gtk",
    }
)
# Interactive controls are returned before decorative labels when the cap is reached.
_ROLE_PRIORITY = {
    "push button": 0,
    "button": 0,
    "toggle button": 0,
    "switch": 0,
    "link": 0,
    "entry": 0,
    "password text": 0,
    "menu item": 1,
    "page tab": 1,
    "check box": 1,
    "radio button": 1,
    "combo box": 1,
    "option": 1,
    "slider": 2,
    "spin button": 2,
    "heading": 3,
    "document web": 3,
    "document frame": 3,
    "text": 4,
}
_CLICK_ACTIONS = ("click", "press", "activate", "toggle", "check", "select", "jump")


class Walker:
    """One snapshot of the AT-SPI desktop, with an element cache for later actions."""

    def __init__(self, Atspi) -> None:
        self.Atspi = Atspi
        self.elements: list[dict] = []
        self.cache: dict[str, object] = {}
        self.windows: list[dict] = []
        self.nodes = 0
        self.max_nodes = 2500
        self.max_depth = 24

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def _safe(call, default=None):
        try:
            return call()
        except Exception:
            return default

    def _states(self, node):
        state_set = self._safe(node.get_state_set)
        if state_set is None:
            return set()
        found: set[str] = set()
        for name in ("ACTIVE", "FOCUSED", "EDITABLE", "SHOWING", "VISIBLE", "SELECTED"):
            state = getattr(self.Atspi.StateType, name, None)
            if state is None:
                continue
            if self._safe(lambda: state_set.contains(state), False):
                found.add(name)
        return found

    def _bounds(self, node, origin: tuple[int, int]) -> tuple[int, int, int, int]:
        if not self._safe(node.is_component, False):
            return 0, 0, 0, 0
        component = self._safe(node.get_component_iface)
        if component is None:
            return 0, 0, 0, 0
        screen = self._safe(
            lambda: component.get_extents(self.Atspi.CoordType.SCREEN)
        )
        if screen is None:
            return 0, 0, 0, 0
        x, y, w, h = int(screen.x), int(screen.y), int(screen.width), int(screen.height)
        # GTK4 reports x=0/y=0 for every child in SCREEN coordinates. WINDOW coordinates
        # are correct, so add the frame origin whenever SCREEN looks like that sentinel.
        window = self._safe(
            lambda: component.get_extents(self.Atspi.CoordType.WINDOW)
        )
        if (x, y) == (0, 0):
            if window is not None and (int(window.x), int(window.y)) != (0, 0):
                x, y = origin[0] + int(window.x), origin[1] + int(window.y)
                w, h = int(window.width), int(window.height)
            elif origin != (0, 0):
                x, y = origin
        # AT-SPI uses INT32_MIN for "no extents" (for example an off-screen shell item).
        if min(x, y, w, h) < -1_000_000 or max(abs(x), abs(y), w, h) > 1_000_000:
            return 0, 0, 0, 0
        if w < 0 or h < 0:
            return 0, 0, 0, 0
        return x, y, w, h

    def _actions(self, node) -> list[str]:
        action = self._safe(node.get_action_iface)
        if action is None:
            return []
        count = self._safe(lambda: int(action.get_n_actions()), 0)
        names: list[str] = []
        for index in range(min(count, 6)):
            name = self._safe(lambda i=index: action.get_action_name(i), "")
            if name:
                names.append(str(name))
        return names

    def _collect(
        self,
        node,
        depth: int,
        app: str,
        window: str,
        origin: tuple[int, int],
        order: list[int],
    ) -> None:
        if depth > self.max_depth or self.nodes >= self.max_nodes:
            return
        self.nodes += 1
        role = str(self._safe(node.get_role_name, "") or "")
        name = " ".join(str(self._safe(node.get_name, "") or "").split())
        states = self._states(node)
        if role in INTERACTIVE_ROLES and (name or role in EDITABLE_ROLES):
            x, y, w, h = self._bounds(node, origin)
            ref = f"@e{order[0]}"
            order[0] += 1
            editable = role in EDITABLE_ROLES and (
                "EDITABLE" in states or role in ("entry", "password text")
            )
            self.cache[ref] = node
            self.elements.append(
                {
                    "ref": ref,
                    "name": name[:90],
                    "role": role,
                    "app": app,
                    "window": window,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "cx": x + w // 2,
                    "cy": y + h // 2,
                    "focused": "FOCUSED" in states,
                    "editable": bool(editable),
                    "actions": self._actions(node),
                }
            )
        child_count = self._safe(lambda: int(node.get_child_count()), 0)
        for index in range(child_count):
            if self.nodes >= self.max_nodes:
                return
            child = self._safe(lambda i=index: node.get_child_at_index(i))
            if child is not None:
                self._collect(child, depth + 1, app, window, origin, order)

    def snapshot(self, max_elements: int, max_nodes: int, max_depth: int) -> dict:
        self.nodes = 0
        self.max_nodes = max(100, min(8000, int(max_nodes)))
        self.max_depth = max(6, min(40, int(max_depth)))
        self.elements = []
        self.cache = {}
        self.windows = []
        order = [1]

        desktop = self.Atspi.get_desktop(0)
        app_count = self._safe(lambda: int(desktop.get_child_count()), 0)
        visible_frames: list[tuple[object, str, str, tuple[int, int], bool]] = []
        for app_index in range(app_count):
            application = self._safe(lambda i=app_index: desktop.get_child_at_index(i))
            if application is None:
                continue
            app_name = str(self._safe(application.get_name, "") or "")
            if app_name in SYSTEM_APPS:
                continue
            frame_count = self._safe(lambda: int(application.get_child_count()), 0)
            for frame_index in range(frame_count):
                frame = self._safe(
                    lambda i=frame_index, a=application: a.get_child_at_index(i)
                )
                if frame is None:
                    continue
                role = str(self._safe(frame.get_role_name, "") or "")
                if role not in WINDOW_ROLES:
                    continue
                states = self._states(frame)
                title = str(self._safe(frame.get_name, "") or "")
                x, y, w, h = self._bounds(frame, (0, 0))
                focused = "ACTIVE" in states or "FOCUSED" in states
                visible = "SHOWING" in states or "VISIBLE" in states
                self.windows.append(
                    {
                        "app": app_name,
                        "title": title,
                        "x": x,
                        "y": y,
                        "w": w,
                        "h": h,
                        "focused": focused,
                        "visible": visible,
                    }
                )
                if visible:
                    visible_frames.append((frame, app_name, title, (x, y), focused))

        # Focused window first: that is what "on my screen" means. Remaining windows are
        # still walked until the element cap is reached.
        visible_frames.sort(key=lambda item: 0 if item[4] else 1)
        for frame, app_name, title, origin, _focused in visible_frames:
            self._collect(frame, 0, app_name, title, origin, order)
            if len(self.elements) >= 240:
                break

        elements = self._rank(self.elements, max_elements)
        # Re-number after ranking so the LLM always sees @e1..@eN with no gaps.
        previous = self.cache
        self.cache = {}
        for index, item in enumerate(elements, start=1):
            old_ref = item["ref"]
            item["ref"] = f"@e{index}"
            if old_ref in previous:
                self.cache[item["ref"]] = previous[old_ref]
        focused_window = next((w for w in self.windows if w["focused"]), None)
        if focused_window is None and self.windows:
            focused_window = next((w for w in self.windows if w["visible"]), None)
        return {
            "ok": True,
            "source": "atspi",
            "windows": self.windows,
            "focused_window": (focused_window or {}).get("title", ""),
            "focused_app": (focused_window or {}).get("app", ""),
            "focused": bool(next((w for w in self.windows if w["focused"]), None)),
            "elements": elements,
            "node_count": self.nodes,
        }

    @staticmethod
    def _rank(elements: list[dict], limit: int) -> list[dict]:
        def key(item: dict) -> tuple:
            return (
                _ROLE_PRIORITY.get(str(item.get("role")), 5),
                0 if item.get("focused") else 1,
                0 if item.get("name") else 1,
                0 if item.get("w") and item.get("h") else 1,
            )

        ranked = sorted(elements, key=key)
        keep_refs = {item["ref"] for item in ranked[: max(1, int(limit))]}
        return [item for item in elements if item["ref"] in keep_refs][: max(1, int(limit))]

    # -- actions -----------------------------------------------------------------
    def click(self, ref: str) -> dict:
        node = self.cache.get(ref)
        if node is None:
            return {"ok": False, "error": "That element is stale; take a new snapshot."}
        action = self._safe(node.get_action_iface)
        if action is not None:
            count = self._safe(lambda: int(action.get_n_actions()), 0)
            chosen = -1
            for index in range(count):
                name = str(
                    self._safe(lambda i=index: action.get_action_name(i), "") or ""
                ).lower()
                if name in _CLICK_ACTIONS:
                    chosen = index
                    break
            if chosen < 0 and count > 0:
                chosen = 0
            if chosen >= 0 and self._safe(lambda: bool(action.do_action(chosen)), False):
                return {"ok": True, "ref": ref, "method": "atspi_action"}
        component = self._safe(node.get_component_iface)
        if component is not None and self._safe(lambda: bool(component.grab_focus()), False):
            return {"ok": True, "ref": ref, "method": "atspi_focus"}
        return {"ok": False, "ref": ref, "error": "AT-SPI has no click action for it."}

    def focus(self, ref: str) -> dict:
        node = self.cache.get(ref)
        if node is None:
            return {"ok": False, "error": "That element is stale; take a new snapshot."}
        component = self._safe(node.get_component_iface)
        if component is not None and self._safe(lambda: bool(component.grab_focus()), False):
            return {"ok": True, "ref": ref, "method": "atspi_focus"}
        return {"ok": False, "ref": ref, "error": "AT-SPI could not focus it."}

    def set_text(self, ref: str, text: str) -> dict:
        node = self.cache.get(ref)
        if node is None:
            return {"ok": False, "error": "That element is stale; take a new snapshot."}
        editable = self._safe(node.get_editable_text_iface)
        if editable is not None and self._safe(
            lambda: bool(editable.set_text_contents(text)), False
        ):
            return {"ok": True, "ref": ref, "method": "atspi_set_text"}
        return {"ok": False, "ref": ref, "error": "AT-SPI cannot set that field."}


def main() -> int:
    Atspi = _load_atspi()
    walker = Walker(Atspi)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            print(json.dumps({"ok": False, "error": "bad json"}), flush=True)
            continue
        cmd = str(request.get("cmd") or "")
        try:
            if cmd == "snapshot":
                result = walker.snapshot(
                    int(request.get("max_elements") or 40),
                    int(request.get("max_nodes") or 2500),
                    int(request.get("max_depth") or 24),
                )
            elif cmd == "click":
                result = walker.click(str(request.get("ref") or ""))
            elif cmd == "focus":
                result = walker.focus(str(request.get("ref") or ""))
            elif cmd == "set_text":
                result = walker.set_text(
                    str(request.get("ref") or ""), str(request.get("text") or "")
                )
            elif cmd == "windows":
                result = walker.snapshot(1, 600, 8)
            elif cmd == "ping":
                result = {"ok": True, "source": "atspi_helper"}
            else:
                result = {"ok": False, "error": f"unknown command {cmd!r}"}
        except Exception as exc:  # never kill the long-lived helper
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
