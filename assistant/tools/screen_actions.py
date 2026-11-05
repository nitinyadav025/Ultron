"""Ultron's hands: semantic click, screen reading and field typing on Wayland.

The click path is a three-tier Observe → Act → Verify ladder:

1. **Tier 1 — AT-SPI semantic action.** ``atspi_helper.py`` (system Python) invokes the
   element's real ``click``/``press`` action. This is the only method that never guesses a
   pixel.
2. **Tier 2 — physical coordinates.** The element's screen-absolute bounding-box centre is
   clicked with ``ydotool mousemove --absolute <x> <y>`` + ``ydotool click 0xC0``. The
   real ydotool syntax has positional coordinates; the ``-x/-y`` form does not exist in
   ydotool 1.0.4 (verified from its man page).
3. **Tier 3 — browser in-page find.** Firefox/Brave/Chrome on Wayland: Ctrl+F, paste the
   visible label, Esc, Enter. This activates a link or button whose DOM text is visible
   even when accessibility exposes no element or coordinates. The live behaviour of
   Enter-after-Esc is **UNVERIFIED** by the coding session and must be checked by Nitin.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable

from assistant.brain.context import BROWSERS, SESSION
from assistant.tools import desktop_control as dc
from assistant.tools.registry import tool
from assistant.vision.screen_watcher import (
    ScreenSnapshot,
    UIElement,
    apply_snapshot,
    find_element,
    format_visible_ui,
    ocr_screen_elements,
    snapshot_screen,
)

logger = logging.getLogger("ultron.screen_actions")

_MAX_TYPE_CHARS = 4000
_BROWSER_FIND_DELAY_S = 0.08
_BROWSER_SELECT_DELAY_S = 0.04
_SNAPSHOT_MAX_ELEMENTS = 40


def _snapshot(**kwargs: Any) -> ScreenSnapshot:
    return _snapshot_fn(**kwargs)


# Tests replace these two hooks: pytest never spawns the helper or clicks the real desktop.
_snapshot_fn: Callable[..., ScreenSnapshot] = snapshot_screen
_atspi_request: Callable[[dict[str, Any]], dict[str, Any]] | None = None
_agent_runner: Callable[[list[str]], Any] | None = None


def _atspi(payload: dict[str, Any]) -> dict[str, Any]:
    if _atspi_request is not None:
        return _atspi_request(payload)
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ULTRON_NO_ATSPI"):
        return {"ok": False, "error": "AT-SPI is disabled for this run."}
    from assistant.vision.screen_watcher import get_atspi_client

    return get_atspi_client().request(payload)


def _agent_desktop_click(native_ref: str) -> tuple[bool, str]:
    binary = shutil.which("agent-desktop")
    if not binary or not native_ref:
        return False, "agent-desktop is unavailable."
    if _agent_runner is not None:
        result = _agent_runner([binary, "click", native_ref])
    else:
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get(
            "ULTRON_NO_AGENT_DESKTOP"
        ):
            return False, "agent-desktop is disabled for this run."
        try:
            result = subprocess.run(
                [binary, "click", native_ref],
                capture_output=True,
                text=True,
                timeout=6.0,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
    if getattr(result, "returncode", 1) != 0:
        return False, "agent-desktop click failed."
    try:
        import json

        payload = json.loads(getattr(result, "stdout", "") or "{}")
    except ValueError:
        return False, "agent-desktop returned invalid JSON."
    return bool(payload.get("ok")), str((payload.get("error") or {}).get("message") or "")


def _elements(force: bool = False) -> list[UIElement]:
    if not force and SESSION.screen_elements:
        return [UIElement.from_dict(item) for item in SESSION.screen_elements]
    snapshot = _snapshot(max_elements=_SNAPSHOT_MAX_ELEMENTS)
    apply_snapshot(snapshot)
    return list(snapshot.elements)


def _semantic_click(element: UIElement) -> tuple[bool, str, str]:
    """Tier 1: the element's own accessibility action."""
    if element.source == "ocr":
        return False, "OCR elements have no accessibility action.", "ocr"
    if element.source == "agent-desktop" and element.native_ref:
        ok, error = _agent_desktop_click(element.native_ref)
        return ok, ("agent-desktop" if ok else error), "agent-desktop"
    reply = _atspi({"cmd": "click", "ref": element.ref})
    if reply.get("ok"):
        return True, str(reply.get("method") or "atspi_action"), "atspi"
    return False, str(reply.get("error") or "AT-SPI click failed."), "atspi"


def _physical_click(x: int, y: int) -> tuple[bool, str]:
    """Tier 2: ydotool absolute move + left click (real 1.0.4 syntax)."""
    if x <= 0 and y <= 0:
        return False, "No usable screen coordinates."
    moved, move_error = dc._ydotool(["mousemove", "--absolute", str(int(x)), str(int(y))])
    if not moved:
        return False, move_error or "The pointer could not move."
    clicked, click_error = dc._ydotool(["click", "0xC0"])
    if not clicked:
        return False, click_error or "The click was not delivered."
    return True, ""


def _browser_name() -> str:
    for candidate in (SESSION.last_browser, SESSION.focused_app, SESSION.last_app):
        key = (candidate or "").strip().lower()
        if key in BROWSERS:
            return key
    return ""


def _focus_browser() -> None:
    browser = _browser_name()
    if browser and shutil.which("wmctrl"):
        try:
            dc._run(["wmctrl", "-a", browser.title()], timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            pass
        if dc._focus_delay_s > 0:
            time.sleep(dc._focus_delay_s)


def _browser_find_and_enter(target: str) -> tuple[bool, str]:
    """Tier 3: Ctrl+F, label, Esc, Enter — activates text visible in the page."""
    if not _browser_name() and not SESSION.active_site:
        return False, "No browser window is active."
    _focus_browser()
    copied = dc._copy_clipboard(target)
    if not dc._ydotool(["key", *dc._combo([dc.KEY_LEFTCTRL, dc.KEY_F])])[0]:
        return False, "The browser find bar could not be opened."
    if _BROWSER_FIND_DELAY_S > 0:
        time.sleep(_BROWSER_FIND_DELAY_S)
    if copied:
        if not dc._ydotool(["key", *dc._combo([dc.KEY_LEFTCTRL, dc.KEY_V])])[0]:
            return False, "The find text could not be pasted."
    else:
        if not dc._ydotool(["type", "--key-delay", "8", "--", target])[0]:
            return False, "The find text could not be typed."
    if _BROWSER_SELECT_DELAY_S > 0:
        time.sleep(_BROWSER_SELECT_DELAY_S)
    if not dc._ydotool(["key", *dc._tap(dc.KEY_ESCAPE)])[0]:
        return False, "The find bar could not be closed."
    if not dc._ydotool(["key", *dc._tap(dc.KEY_ENTER)])[0]:
        return False, "Enter could not be pressed."
    return True, ""


def _verify_click(element: UIElement) -> bool:
    """Re-observe after a click. Never blocks a reply when the desktop is busy."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ULTRON_NO_SCREEN_WATCHER"):
        return False
    try:
        time.sleep(0.18)
        after = _snapshot(max_elements=_SNAPSHOT_MAX_ELEMENTS, allow_ocr=False)
    except Exception:
        return False
    if not after.elements:
        return False
    same = find_element(element.name, after.elements) if element.name else None
    return same is None


def _element_label(element: UIElement) -> str:
    return element.name or element.ref


@tool(risk_level="safe", read_only=False)
def click_element(target: str) -> dict[str, Any]:
    """Click a visible button, link, tab or menu item by @ref or label."""
    cleaned = re.sub(r"\s+", " ", (target or "").strip().strip("\"'"))
    if not cleaned:
        return {"ok": False, "error": "Say which element to click."}
    elements = _elements()
    element = find_element(cleaned, elements)
    if element is None:
        # Layer C on demand: a canvas/video/app that exposes nothing on AT-SPI still has
        # visible words. This screenshot happens only because the user asked to click.
        ocr_snapshot = ocr_screen_elements()
        if ocr_snapshot.elements:
            element = find_element(cleaned, ocr_snapshot.elements)
    attempts: list[dict[str, Any]] = []

    if element is not None:
        ok, detail, method = _semantic_click(element)
        attempts.append({"tier": 1, "method": method, "ok": ok, "detail": detail})
        if ok:
            SESSION.note_action(f"clicked {_element_label(element)}")
            return {
                "ok": True,
                "target": cleaned,
                "element": element.as_dict(),
                "tier": 1,
                "method": method,
                "verified": False,
                "reply": f"I've clicked {_element_label(element)}.",
            }

        if element.cx > 0 and element.cy > 0:
            ok, error = _physical_click(element.cx, element.cy)
            attempts.append({"tier": 2, "method": "ydotool", "ok": ok, "detail": error})
            if ok:
                verified = _verify_click(element)
                SESSION.note_action(f"clicked {_element_label(element)}")
                return {
                    "ok": True,
                    "target": cleaned,
                    "element": element.as_dict(),
                    "tier": 2,
                    "method": "ydotool",
                    "coordinates": [element.cx, element.cy],
                    "verified": verified,
                    "reply": f"I've clicked {_element_label(element)}.",
                }

    ok, error = _browser_find_and_enter(cleaned)
    attempts.append({"tier": 3, "method": "browser_find", "ok": ok, "detail": error})
    if ok:
        SESSION.note_action(f"clicked {cleaned}")
        return {
            "ok": True,
            "target": cleaned,
            "tier": 3,
            "method": "browser_find",
            "verified": False,
            "reply": f"I've activated {cleaned} in the page.",
        }

    return {
        "ok": False,
        "target": cleaned,
        "found": element.as_dict() if element is not None else None,
        "attempts": attempts,
        "error": (
            f"I couldn't click {cleaned}. "
            "I couldn't find it on the accessibility tree, at a coordinate, or in the page."
        ),
    }


@tool(risk_level="safe", read_only=True)
def read_screen(query: str = "") -> dict[str, Any]:
    """Read what is visible on the screen, or answer a question about it."""
    cleaned_query = re.sub(r"\s+", " ", (query or "").strip().strip("\"'"))
    elements = _elements(force=not SESSION.screen_elements)
    focused_window = SESSION.focused_window or (SESSION.open_windows[0] if SESSION.open_windows else "")
    focused_app = SESSION.focused_app
    site = SESSION.active_site
    windows = list(SESSION.open_windows)

    if cleaned_query:
        match = find_element(cleaned_query, elements)
        if match is not None:
            label = match.name or match.role
            reply = f"Yes, I can see {label} on the screen."
            return {
                "ok": True,
                "query": cleaned_query,
                "found": match.as_dict(),
                "focused_window": focused_window,
                "focused_app": focused_app,
                "site": site,
                "windows": windows,
                "reply": reply,
            }
        partial = [
            item for item in elements if cleaned_query.lower() in item.name.lower()
        ][:3]
        if partial:
            names = ", ".join(item.name for item in partial)
            return {
                "ok": True,
                "query": cleaned_query,
                "matches": [item.as_dict() for item in partial],
                "focused_window": focused_window,
                "focused_app": focused_app,
                "site": site,
                "windows": windows,
                "reply": f"I can see {names}.",
            }
        return {
            "ok": True,
            "query": cleaned_query,
            "found": None,
            "focused_window": focused_window,
            "focused_app": focused_app,
            "site": site,
            "windows": windows,
            "reply": f"I don't see {cleaned_query} on the screen right now.",
        }

    labels = [
        item.name
        for item in elements
        if item.name and (item.clickable or item.editable)
    ][:5]
    parts: list[str] = []
    if focused_window:
        where = f'You are in "{focused_window}"'
        if site:
            where += f" on {site.title()}"
        parts.append(where + ".")
    elif site:
        parts.append(f"You are on {site.title()}.")
    elif windows:
        parts.append(f"You have {len(windows)} windows open.")
    if labels:
        parts.append("I can see " + ", ".join(labels) + ".")
    elif elements:
        parts.append(f"I can see {len(elements)} text items on the screen.")
    else:
        parts.append("I can't read anything on the screen right now.")
    return {
        "ok": bool(elements or focused_window or windows),
        "query": "",
        "focused_window": focused_window,
        "focused_app": focused_app,
        "site": site,
        "windows": windows,
        "elements": len(elements),
        "visible_ui": format_visible_ui(elements),
        "reply": " ".join(parts),
    }


def _focus_element(element: UIElement) -> tuple[bool, str]:
    if element.source == "agent-desktop" and element.native_ref:
        return _agent_desktop_click(element.native_ref)
    reply = _atspi({"cmd": "focus", "ref": element.ref})
    if reply.get("ok"):
        return True, ""
    ok, error, _method = _semantic_click(element)
    if ok:
        return True, ""
    if element.cx > 0 and element.cy > 0:
        return _physical_click(element.cx, element.cy)
    return False, error or "The field could not be focused."


def _paste_into_focused(text: str, press_enter: bool = False) -> tuple[bool, str, bool]:
    """Clipboard paste without moving window focus (the field was just focused)."""
    if dc._copy_clipboard(text):
        if dc._clipboard_paste_delay_s > 0:
            time.sleep(dc._clipboard_paste_delay_s)
        pasted, error = dc._ydotool(["key", *dc._combo([dc.KEY_LEFTCTRL, dc.KEY_V])])
        if pasted:
            if press_enter:
                dc._ydotool(["key", *dc._tap(dc.KEY_ENTER)])
            return True, "", True
        paste_error = error
    else:
        paste_error = "The clipboard is unavailable."
    typed, type_error = dc._ydotool(["type", "--key-delay", "8", "--", text])
    if typed:
        if press_enter:
            dc._ydotool(["key", *dc._tap(dc.KEY_ENTER)])
        return True, "", False
    return False, type_error or paste_error, False


@tool(risk_level="safe", read_only=False)
def focus_and_type(target_field: str, text: str) -> dict[str, Any]:
    """Focus a visible input field and paste text into it."""
    field = re.sub(r"\s+", " ", (target_field or "").strip().strip("\"'"))
    cleaned = re.sub(r"\s+", " ", (text or "").replace("\r", " ").replace("\n", " ")).strip()
    if not cleaned:
        return {"ok": False, "error": "There was no text to type."}
    if len(cleaned) > _MAX_TYPE_CHARS:
        return {"ok": False, "error": f"That text is longer than {_MAX_TYPE_CHARS} characters."}
    if dc._looks_secret(cleaned):
        return {"ok": False, "error": "I won't type passwords, keys or tokens."}

    elements = _elements()
    element = find_element(field, elements, prefer_editable=True) if field else None
    if element is None:
        element = next(
            (item for item in elements if item.editable and item.focused), None
        )
    if element is None:
        element = next((item for item in elements if item.editable), None)
    if element is None:
        return {
            "ok": False,
            "target_field": field,
            "error": f"I couldn't find an input field for {field or 'that'}.",
        }

    focused, focus_error = _focus_element(element)
    if not focused:
        return {
            "ok": False,
            "target_field": field,
            "element": element.as_dict(),
            "error": focus_error or "I couldn't focus that field.",
        }
    ok, error, used_clipboard = _paste_into_focused(cleaned)
    if not ok:
        return {
            "ok": False,
            "target_field": field,
            "element": element.as_dict(),
            "error": error or "I couldn't type into that field.",
        }
    SESSION.last_typed_text = cleaned
    SESSION.note_action(f"typed into {element.name or field or 'a field'}")
    return {
        "ok": True,
        "target_field": field,
        "element": element.as_dict(),
        "method": "clipboard_paste" if used_clipboard else "ydotool",
        "text_chars": len(cleaned),
        "reply": f"I've typed that into the {element.name or field or 'field'}.",
    }
