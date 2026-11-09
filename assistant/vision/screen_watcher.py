"""Ultron's eyes: a continuous, cheap snapshot of the visible UI.

Three independent layers, in the order that is honest on Fedora 44 GNOME Wayland:

* **Layer B (primary)** — a persistent ``/usr/bin/python3`` AT-SPI2 helper
  (``assistant/vision/atspi_helper.py``). The venv has no ``gi``; the system Python does.
* **Layer A (optional)** — the Vercel ``agent-desktop`` CLI, which speaks the same
  ``@ref`` snapshot/act protocol. It is used when explicitly preferred or when the native
  helper is unavailable.
* **Layer C (fallback)** — screenshot + ``tesseract --psm 11 tsv`` for canvases, videos and
  apps that expose nothing on the accessibility bus.

The snapshot is stored in ``SESSION.screen_elements`` and summarised into the
``[Visible UI: @e1:button:"Show more", ...]`` tag on the current user message. It never
enters the static system prompt, so llama-server's KV prefix stays reusable.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from assistant.brain.context import SESSION, app_name_for_window, is_launcher_window, is_ultron_window

logger = logging.getLogger("ultron.vision")

ATSPI_HELPER = Path(__file__).with_name("atspi_helper.py")
SYSTEM_PYTHON = "/usr/bin/python3"
SCREENSHOT_PATH = Path(
    os.environ.get("ULTRON_SCREENSHOT_PATH") or "/tmp/ultron_screen.png"
)
WATCH_INTERVAL_S = 1.2

# Roles shown to the LLM in the compact Visible UI line, in priority order.
_UI_ROLES = (
    "push button",
    "button",
    "toggle button",
    "link",
    "entry",
    "password text",
    "page tab",
    "menu item",
    "check box",
    "combo box",
    "heading",
    "document web",
    "text",
)
_ROLE_DISPLAY = {
    "push button": "button",
    "button": "button",
    "toggle button": "toggle",
    "switch": "toggle",
    "password text": "entry",
    "text": "text",
    "document web": "document",
    "document frame": "document",
}
_SYSTEM_APPS = frozenset(
    {
        "gnome-shell",
        "ibus-extension-gtk3",
        "evolution-alarm-notify",
        "gsconnect",
        "gjs-console",
        "xdg-desktop-portal-gtk",
    }
)
# GTK/shell window chrome that carries no user meaning. Showing these produced the live
# "New Terminal, Show Open Tabs, Main Menu, Main Menu, Minimize" reply.
_CHROME_LABELS = frozenset(
    {
        "minimize",
        "maximize",
        "restore",
        "close",
        "main menu",
        "new terminal",
        "show open tabs",
        "window menu",
        "toggle maximize",
        "application menu",
        "maximize window",
        "minimize window",
        "restore window",
        "close window",
        "open menu",
    }
)


def is_meaningful_element(element: "UIElement") -> bool:
    """False for generic window chrome and Ultron's own dashboard/launcher UI."""
    name = re.sub(r"\s+", " ", (element.name or "")).strip().lower()
    if name in _CHROME_LABELS:
        return False
    for window in (element.window, element.app):
        if window and (is_ultron_window(window) or is_launcher_window(window)):
            return False
    return True


def filter_ui_elements(elements: Iterable["UIElement"]) -> list["UIElement"]:
    """Drop chrome/noise and duplicate labels (never "Main Menu, Main Menu")."""
    kept: list[UIElement] = []
    seen: set[tuple[str, str]] = set()
    for element in elements:
        if not is_meaningful_element(element):
            continue
        key = ((element.name or "").strip().lower(), element.role)
        if key in seen:
            continue
        seen.add(key)
        kept.append(element)
    return kept


@dataclass(frozen=True)
class UIElement:
    """One visible UI element, addressed as ``@eN`` in the current snapshot."""

    ref: str
    name: str
    role: str
    app: str = ""
    window: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    focused: bool = False
    editable: bool = False
    actions: tuple[str, ...] = ()
    source: str = "atspi"
    native_ref: str = ""

    @property
    def cx(self) -> int:
        return self.x + self.w // 2 if self.w > 0 else 0

    @property
    def cy(self) -> int:
        return self.y + self.h // 2 if self.h > 0 else 0

    @property
    def clickable(self) -> bool:
        return bool(self.actions) or self.role in (
            "push button",
            "button",
            "toggle button",
            "link",
            "menu item",
            "page tab",
            "check box",
            "combo box",
            "option",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "name": self.name,
            "role": self.role,
            "app": self.app,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "cx": self.cx,
            "cy": self.cy,
            "focused": self.focused,
            "editable": self.editable,
            "actions": list(self.actions),
            "source": self.source,
            "native_ref": self.native_ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UIElement":
        return cls(
            ref=str(data.get("ref") or ""),
            name=str(data.get("name") or ""),
            role=str(data.get("role") or ""),
            app=str(data.get("app") or ""),
            window=str(data.get("window") or ""),
            x=int(data.get("x") or 0),
            y=int(data.get("y") or 0),
            w=int(data.get("w") or 0),
            h=int(data.get("h") or 0),
            focused=bool(data.get("focused")),
            editable=bool(data.get("editable")),
            actions=tuple(str(item) for item in data.get("actions") or ()),
            source=str(data.get("source") or "atspi"),
            native_ref=str(data.get("native_ref") or ""),
        )


@dataclass
class ScreenSnapshot:
    """Everything the eyes know after one refresh."""

    elements: list[UIElement] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    focused_window: str = ""
    focused_app: str = ""
    source: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.elements or self.windows)


# --------------------------------------------------------------------------------------
# Layer B: native AT-SPI helper client
# --------------------------------------------------------------------------------------


class AtspiClient:
    """JSON-lines client for ``atspi_helper.py`` running under the system Python."""

    def __init__(
        self,
        *,
        python: str = SYSTEM_PYTHON,
        helper: Path = ATSPI_HELPER,
        process_factory: Callable[..., Any] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.python = python
        self.helper = Path(helper)
        self._process_factory = process_factory or subprocess.Popen
        self._process: Any = None
        self._lock = threading.Lock()
        self._log = log or logger.info

    def available(self) -> bool:
        return Path(self.python).exists() and self.helper.is_file()

    def _start(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        self.close()
        try:
            self._process = self._process_factory(
                [self.python, "-u", str(self.helper)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                close_fds=True,
            )
        except (OSError, ValueError, TypeError) as exc:
            self._log(f"AT-SPI helper could not start: {type(exc).__name__}")
            self._process = None
            return False
        return True

    def request(self, payload: dict[str, Any], timeout: float = 4.0) -> dict[str, Any]:
        """Send one command and read exactly one JSON reply."""
        with self._lock:
            if not self.available() or not self._start():
                return {"ok": False, "error": "AT-SPI helper unavailable."}
            process = self._process
            try:
                process.stdin.write(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                process.stdin.flush()
                ready, _, _ = select.select([process.stdout], [], [], timeout)
                if not ready:
                    # Kill the wedged helper so the next refresh starts a clean one.
                    self.close()
                    return {"ok": False, "error": "AT-SPI helper timed out."}
                line = process.stdout.readline()
            except (OSError, ValueError, TypeError) as exc:
                self.close()
                return {"ok": False, "error": f"AT-SPI helper failed: {type(exc).__name__}"}
            if not line:
                self.close()
                return {"ok": False, "error": "AT-SPI helper exited."}
        try:
            data = json.loads(line)
        except ValueError:
            return {"ok": False, "error": "AT-SPI helper returned invalid JSON."}
        return data if isinstance(data, dict) else {"ok": False, "error": "bad reply"}

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        for stream in (getattr(process, "stdin", None), getattr(process, "stdout", None)):
            try:
                stream.close()  # type: ignore[union-attr]
            except Exception:
                pass
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


_CLIENT: AtspiClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_atspi_client() -> AtspiClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = AtspiClient()
        return _CLIENT


def stop_atspi_client() -> None:
    """Stop the helper (also called from atexit and the live test checklist)."""
    global _CLIENT
    with _CLIENT_LOCK:
        client, _CLIENT = _CLIENT, None
    if client is not None:
        client.close()


def atspi_available() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ULTRON_NO_ATSPI"):
        return False
    return get_atspi_client().available()


# --------------------------------------------------------------------------------------
# Parsing (pure functions: every branch below is unit-tested with fixtures)
# --------------------------------------------------------------------------------------


def parse_atspi_snapshot(payload: dict[str, Any]) -> ScreenSnapshot:
    """Turn an ``atspi_helper`` reply into a ScreenSnapshot."""
    elements = [
        UIElement(
            ref=str(item.get("ref") or ""),
            name=str(item.get("name") or ""),
            role=str(item.get("role") or ""),
            app=str(item.get("app") or ""),
            window=str(item.get("window") or ""),
            x=int(item.get("x") or 0),
            y=int(item.get("y") or 0),
            w=int(item.get("w") or 0),
            h=int(item.get("h") or 0),
            focused=bool(item.get("focused")),
            editable=bool(item.get("editable")),
            actions=tuple(str(action) for action in item.get("actions") or ()),
            source="atspi",
        )
        for item in payload.get("elements") or []
        if item.get("ref")
    ]
    elements = filter_ui_elements(elements)
    windows: list[str] = []
    for item in payload.get("windows") or []:
        if str(item.get("app") or "") in _SYSTEM_APPS:
            continue
        title = str(item.get("title") or item.get("app") or "").strip()
        if title and title not in windows:
            windows.append(title)
    return ScreenSnapshot(
        elements=elements,
        windows=windows,
        focused_window=str(payload.get("focused_window") or ""),
        focused_app=str(payload.get("focused_app") or ""),
        source="atspi",
    )


_AGENT_ROLE_MAP = {
    "button": "push button",
    "switch": "toggle button",
    "checkbox": "check box",
    "textfield": "entry",
    "tab": "page tab",
    "document": "document web",
}


def _flatten_agent_tree(node: dict[str, Any], out: list[dict[str, Any]]) -> None:
    if not isinstance(node, dict):
        return
    if node.get("ref_id"):
        out.append(node)
    for child in node.get("children") or []:
        _flatten_agent_tree(child, out)


def parse_agent_desktop_snapshot(
    payload: dict[str, Any], *, app: str = ""
) -> ScreenSnapshot:
    """Turn one ``agent-desktop snapshot`` JSON reply into a ScreenSnapshot."""
    data = payload.get("data") or {}
    tree = data.get("tree") or {}
    rows: list[dict[str, Any]] = []
    _flatten_agent_tree(tree, rows)
    elements: list[UIElement] = []
    for row in rows:
        name = " ".join(str(row.get("name") or row.get("description") or "").split())
        role = str(row.get("role") or "")
        bounds = row.get("bounds") or {}
        if not isinstance(bounds, dict):
            bounds = {}
        actions = tuple(str(action) for action in row.get("available_actions") or ())
        if not name and not actions:
            continue
        mapped = _AGENT_ROLE_MAP.get(role, role)
        elements.append(
            UIElement(
                ref=f"@e{len(elements) + 1}",
                name=name[:90],
                role=mapped,
                app=app or str(data.get("app") or ""),
                window=str((data.get("window") or {}).get("title") or ""),
                x=int(bounds.get("x") or 0),
                y=int(bounds.get("y") or 0),
                w=int(bounds.get("width") or bounds.get("w") or 0),
                h=int(bounds.get("height") or bounds.get("h") or 0),
                editable=mapped in ("entry", "text", "document web"),
                actions=actions,
                source="agent-desktop",
                native_ref=str(row.get("ref_id") or ""),
            )
        )
    window = data.get("window") or {}
    title = str(window.get("title") or data.get("app") or "")
    return ScreenSnapshot(
        elements=filter_ui_elements(elements),
        windows=[title] if title else [],
        focused_window=title,
        focused_app=app or str(data.get("app") or ""),
        source="agent-desktop",
    )


def parse_tesseract_tsv(
    tsv: str, *, offset_x: int = 0, offset_y: int = 0, min_confidence: float = 30.0
) -> list[UIElement]:
    """Parse ``tesseract ... tsv`` output into word-level clickable text elements."""
    elements: list[UIElement] = []
    seen: set[tuple[str, int, int]] = set()
    for line in (tsv or "").splitlines():
        fields = line.split("\t")
        if len(fields) < 12 or fields[0] != "5":
            continue
        try:
            left, top, width, height = (
                int(fields[6]),
                int(fields[7]),
                int(fields[8]),
                int(fields[9]),
            )
            confidence = float(fields[10])
        except (TypeError, ValueError):
            continue
        text = " ".join(fields[11].split())
        if not text or confidence < min_confidence or len(text) < 2:
            continue
        key = (text.lower(), left, top)
        if key in seen:
            continue
        seen.add(key)
        elements.append(
            UIElement(
                ref=f"@e{len(elements) + 1}",
                name=text[:60],
                role="text",
                x=left + offset_x,
                y=top + offset_y,
                w=width,
                h=height,
                source="ocr",
            )
        )
    return elements


# --------------------------------------------------------------------------------------
# Layer A: agent-desktop adapter
# --------------------------------------------------------------------------------------


def _run_tool(
    argv: list[str], timeout: float = 4.0
) -> subprocess.CompletedProcess[str] | None:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ULTRON_NO_AGENT_DESKTOP"):
        return None
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def agent_desktop_available() -> bool:
    return shutil.which("agent-desktop") is not None


def agent_desktop_snapshot(
    app: str = "",
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ScreenSnapshot:
    """Layer A: query the Vercel agent-desktop CLI, if it is installed."""
    binary = shutil.which("agent-desktop")
    if not binary:
        return ScreenSnapshot(source="agent-desktop", errors=["agent-desktop not installed."])
    run = runner or (lambda argv, **kwargs: _run_tool(argv, kwargs.get("timeout", 4.0)))
    app_name = app
    if not app_name:
        windows = _run_tool([binary, "list-windows"], 3.0) if runner is None else None
        if windows is not None and windows.returncode == 0:
            try:
                data = json.loads(windows.stdout or "{}").get("data") or []
            except ValueError:
                data = []
            if isinstance(data, list):
                focused = next(
                    (
                        item
                        for item in data
                        if isinstance(item, dict)
                        and item.get("is_focused")
                        and str(item.get("app_name") or "") not in _SYSTEM_APPS
                    ),
                    None,
                )
                choice = focused or next(
                    (
                        item
                        for item in data
                        if isinstance(item, dict)
                        and str(item.get("app_name") or "") not in _SYSTEM_APPS
                    ),
                    None,
                )
                app_name = str((choice or {}).get("app_name") or "")
    argv = [binary, "snapshot", "-i", "--compact", "--include-bounds"]
    if app_name:
        argv.extend(["--app", app_name])
    result = run(argv, timeout=6.0)
    if result is None or getattr(result, "returncode", 1) != 0:
        return ScreenSnapshot(
            source="agent-desktop",
            errors=["agent-desktop snapshot failed."],
        )
    try:
        payload = json.loads(getattr(result, "stdout", "") or "{}")
    except ValueError:
        return ScreenSnapshot(source="agent-desktop", errors=["agent-desktop bad JSON."])
    if not payload.get("ok"):
        error = str((payload.get("error") or {}).get("message") or "agent-desktop failed.")
        return ScreenSnapshot(source="agent-desktop", errors=[error])
    return parse_agent_desktop_snapshot(payload, app=app_name)


# --------------------------------------------------------------------------------------
# Layer C: screenshot + OCR
# --------------------------------------------------------------------------------------


def _default_screenshot(path: Path) -> bool:
    if shutil.which("gnome-screenshot"):
        result = _run_tool(["gnome-screenshot", "-f", str(path)], 6.0)
        if result is not None and getattr(result, "returncode", 1) == 0 and path.exists():
            return True
    if shutil.which("gdbus"):
        result = _run_tool(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell.Screenshot",
                "--object-path",
                "/org/gnome/Shell/Screenshot",
                "--method",
                "org.gnome.Shell.Screenshot.Screenshot",
                "true",
                "false",
                str(path),
            ],
            6.0,
        )
        return bool(
            result is not None
            and getattr(result, "returncode", 1) == 0
            and path.exists()
        )
    return False


def _default_ocr(path: Path) -> str:
    binary = shutil.which("tesseract")
    if not binary:
        return ""
    result = _run_tool([binary, str(path), "stdout", "--psm", "11", "tsv"], 8.0)
    if result is None or getattr(result, "returncode", 1) != 0:
        return ""
    return getattr(result, "stdout", "") or ""


def ocr_screen_elements(
    *,
    screenshot_path: Path | None = None,
    screenshot_runner: Callable[[Path], bool] | None = None,
    ocr_runner: Callable[[Path], str] | None = None,
    keep_screenshot: bool = False,
) -> ScreenSnapshot:
    """Layer C: screenshot the screen and read every visible word with tesseract."""
    if os.environ.get("PYTEST_CURRENT_TEST") and screenshot_runner is None:
        return ScreenSnapshot(source="ocr", errors=["OCR disabled under pytest."])
    if shutil.which("tesseract") is None and ocr_runner is None:
        return ScreenSnapshot(source="ocr", errors=["tesseract not installed."])
    path = Path(screenshot_path or SCREENSHOT_PATH)
    shot = screenshot_runner or _default_screenshot
    ocr = ocr_runner or _default_ocr
    temporary = screenshot_path is None
    try:
        if not shot(path):
            return ScreenSnapshot(source="ocr", errors=["screenshot failed."])
        text = ocr(path)
    finally:
        if temporary and not keep_screenshot:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    elements = parse_tesseract_tsv(text or "")
    if not elements:
        return ScreenSnapshot(source="ocr", errors=["tesseract found no words."])
    return ScreenSnapshot(elements=elements, source="ocr")


# --------------------------------------------------------------------------------------
# snapshot_screen: the three-layer gate
# --------------------------------------------------------------------------------------


def snapshot_screen(
    *,
    max_elements: int = 40,
    prefer_agent_desktop: bool = False,
    allow_ocr: bool = True,
    atspi_client: AtspiClient | None = None,
    agent_runner: Callable[..., Any] | None = None,
    screenshot_runner: Callable[[Path], bool] | None = None,
    ocr_runner: Callable[[Path], str] | None = None,
) -> ScreenSnapshot:
    """Return the visible UI using AT-SPI, agent-desktop, then OCR."""
    errors: list[str] = []
    limit = max(1, min(80, int(max_elements)))
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        and atspi_client is None
        and agent_runner is None
        and screenshot_runner is None
    ):
        return ScreenSnapshot(
            source="", errors=["snapshot_screen is disabled under pytest without fakes."]
        )

    def finish(snapshot: ScreenSnapshot) -> ScreenSnapshot:
        snapshot.elements = filter_ui_elements(snapshot.elements)[:limit]
        for index, element in enumerate(snapshot.elements, start=1):
            object.__setattr__(element, "ref", f"@e{index}")
        snapshot.errors = errors + list(snapshot.errors)
        return snapshot

    if prefer_agent_desktop and agent_desktop_available():
        layer_a = agent_desktop_snapshot(runner=agent_runner)
        if layer_a.elements:
            return finish(layer_a)
        errors.extend(layer_a.errors)

    client = atspi_client or get_atspi_client()
    atspi_succeeded = False
    if client.available():
        reply = client.request(
            {
                "cmd": "snapshot",
                "max_elements": limit,
                "max_nodes": 2500,
                "max_depth": 24,
            }
        )
        if reply.get("ok"):
            atspi_succeeded = True
            layer_b = parse_atspi_snapshot(reply)
            if layer_b.elements:
                return finish(layer_b)
            errors.extend(layer_b.errors or ["AT-SPI found no interactive elements."])
        else:
            errors.append(str(reply.get("error") or "AT-SPI failed."))
    else:
        errors.append("AT-SPI helper unavailable.")

    # agent-desktop reads the same AT-SPI bus. When the native helper already answered,
    # re-querying it every 1.2 s only wastes a process; use it only when AT-SPI is broken.
    if not prefer_agent_desktop and agent_desktop_available() and not atspi_succeeded:
        layer_a = agent_desktop_snapshot(runner=agent_runner)
        if layer_a.elements:
            return finish(layer_a)
        errors.extend(layer_a.errors)

    if allow_ocr:
        layer_c = ocr_screen_elements(
            screenshot_runner=screenshot_runner, ocr_runner=ocr_runner
        )
        if layer_c.elements:
            return finish(layer_c)
        errors.extend(layer_c.errors)
    return ScreenSnapshot(source="", errors=errors)


# --------------------------------------------------------------------------------------
# Matching and compact summaries
# --------------------------------------------------------------------------------------


def _clean_target(text: str) -> str:
    cleaned = str(text or "").strip().strip("\"'")
    cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+(?:button|link|tab|option|field|box|area|section)$", "", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def find_element(
    target: str,
    elements: Iterable[UIElement | dict[str, Any]] | None = None,
    *,
    prefer_editable: bool = False,
) -> UIElement | None:
    """Find the best element by ``@ref``, exact label, fuzzy label or role."""
    rows = [
        item if isinstance(item, UIElement) else UIElement.from_dict(item)
        for item in (elements if elements is not None else SESSION.screen_elements)
    ]
    if not rows:
        return None
    raw = str(target or "").strip()
    if not raw:
        return None
    wanted_ref = raw.lower().lstrip("@")
    for item in rows:
        if item.ref.lower().lstrip("@") == wanted_ref:
            return item
    wanted = _clean_target(raw)
    if not wanted:
        return None

    role_words = {
        "button": ("push button", "button"),
        "toggle": ("toggle button",),
        "link": ("link",),
        "entry": ("entry", "password text"),
        "field": ("entry", "password text"),
        "search": ("entry", "password text"),
        "tab": ("page tab",),
    }
    candidates = rows
    if prefer_editable:
        editable = [row for row in rows if row.editable]
        if editable:
            candidates = editable
    if wanted in role_words:
        for row in candidates:
            if row.role in role_words[wanted]:
                return row

    exact = [row for row in candidates if row.name.strip().lower() == wanted]
    if exact:
        return next((row for row in exact if row.focused), exact[0])

    contains = [
        row
        for row in candidates
        if wanted and (wanted in row.name.lower() or row.name.lower() in wanted)
    ]
    if contains:
        return next((row for row in contains if row.clickable), contains[0])

    import difflib

    best: tuple[float, UIElement] | None = None
    for row in candidates:
        if not row.name:
            continue
        score = difflib.SequenceMatcher(None, wanted, row.name.lower()).ratio()
        if row.clickable:
            score += 0.04
        if best is None or score > best[0]:
            best = (score, row)
    if best is not None and best[0] >= 0.72:
        return best[1]
    return None


def format_visible_ui(
    elements: Iterable[UIElement | dict[str, Any]] | None = None,
    *,
    limit: int = 12,
    max_name: int = 28,
) -> str:
    """Compact ``@e1:button:"Show more"`` listing for the per-turn context tag."""
    rows = [
        item if isinstance(item, UIElement) else UIElement.from_dict(item)
        for item in (elements if elements is not None else SESSION.screen_elements)
    ]
    shown: list[str] = []
    seen: set[str] = set()
    for item in rows:
        if len(shown) >= limit:
            break
        if not item.name and not item.editable:
            continue
        if item.role not in _UI_ROLES and not item.editable and not item.clickable:
            continue
        if not is_meaningful_element(item):
            continue
        role = _ROLE_DISPLAY.get(item.role, item.role or "item")
        name = re.sub(r"\s+", " ", item.name).strip()[:max_name]
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if name:
            shown.append(f'{item.ref}:{role}:"{name}"')
        else:
            shown.append(f"{item.ref}:{role}")
    return ", ".join(shown)


def visible_ui_line(limit: int = 12) -> str:
    return format_visible_ui(limit=limit)


def apply_snapshot(snapshot: ScreenSnapshot) -> None:
    """Publish a snapshot into SESSION (never to disk)."""
    SESSION.screen_elements = [item.as_dict() for item in snapshot.elements[:40]]
    SESSION.screen_source = snapshot.source
    SESSION.screen_elements_at = time.monotonic()
    titles = [title for title in snapshot.windows if title]
    if not titles and snapshot.focused_window:
        titles = [snapshot.focused_window]
    focused_app = snapshot.focused_app
    if focused_app:
        focused_app = (
            app_name_for_window(focused_app, focused_app, snapshot.focused_window)
            or focused_app.lower()
        )
    if titles:
        SESSION.note_screen(
            titles,
            focused_window=snapshot.focused_window,
            focused_app=focused_app,
        )


# --------------------------------------------------------------------------------------
# Continuous watcher
# --------------------------------------------------------------------------------------


class ScreenWatcher(threading.Thread):
    """Daemon thread that refreshes the snapshot every ``interval`` seconds."""

    def __init__(
        self,
        *,
        interval: float = WATCH_INTERVAL_S,
        snapshot_fn: Callable[[], ScreenSnapshot] | None = None,
        apply: bool = True,
    ) -> None:
        super().__init__(name="ultron-screen-watcher", daemon=True)
        self.interval = max(0.3, float(interval))
        # Continuous watching stays on the accessibility bus. A screenshot + OCR is only
        # taken when the user explicitly asks (read_screen / click_element), so a 1.2 s
        # refresh never captures the screen in the background.
        self.snapshot_fn = snapshot_fn or (lambda: snapshot_screen(allow_ocr=False))
        self.apply = apply
        self._stop = threading.Event()
        self.last_snapshot: ScreenSnapshot | None = None
        self.last_error = ""

    def refresh(self) -> ScreenSnapshot | None:
        try:
            snapshot = self.snapshot_fn()
        except Exception as exc:  # a watcher must never kill the listener
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Screen watcher refresh failed: %s", type(exc).__name__)
            return None
        if not isinstance(snapshot, ScreenSnapshot):
            return None
        self.last_snapshot = snapshot
        if self.apply:
            apply_snapshot(snapshot)
        return snapshot

    def run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


_WATCHER: ScreenWatcher | None = None
_WATCHER_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def start_screen_watcher(
    interval: float = WATCH_INTERVAL_S,
    *,
    snapshot_fn: Callable[[], ScreenSnapshot] | None = None,
) -> ScreenWatcher | None:
    """Start one watcher per process; tests and benchmark never start a real one."""
    global _WATCHER, _ATEXIT_REGISTERED
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ULTRON_NO_SCREEN_WATCHER"):
        return None
    with _WATCHER_LOCK:
        if _WATCHER is not None and _WATCHER.is_alive():
            return _WATCHER
        _WATCHER = ScreenWatcher(interval=interval, snapshot_fn=snapshot_fn)
        _WATCHER.start()
        if not _ATEXIT_REGISTERED:
            atexit.register(stop_screen_watcher)
            _ATEXIT_REGISTERED = True
        logger.info("Screen watcher started (%.1fs).", _WATCHER.interval)
        return _WATCHER


def stop_screen_watcher() -> None:
    global _WATCHER
    with _WATCHER_LOCK:
        watcher, _WATCHER = _WATCHER, None
    if watcher is not None:
        watcher.stop()
        if watcher is not threading.current_thread():
            watcher.join(timeout=2.0)
        logger.info("Screen watcher stopped.")
    stop_atspi_client()
