"""Small session memory shared by tools and the fast path.

It stores what a follow-up needs (last app, browser, query, typed text, recent turns) plus
a live screen snapshot. On GNOME Wayland ``wmctrl -l`` returns no native windows, so the
snapshot uses three layers:

1. ``org.gnome.Shell.Introspect.GetWindows`` (native Wayland windows and the focused one);
2. ``wmctrl -l`` (XWayland windows);
3. running GUI processes plus the session's own last app/browser/site.

Nothing is written to disk, so "close it" and "open the first link" work only inside the
current session.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("ultron.context")

BROWSERS = frozenset({"firefox", "brave", "chrome"})

# Window-title keywords -> site key. Order matters: the first match wins.
SITE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("youtube", "youtube"),
    ("github", "github"),
    ("instagram", "instagram"),
    ("discord", "discord"),
    ("visual studio code", "vscode"),
    ("vs code", "vscode"),
    ("reddit", "reddit"),
    ("wikipedia", "wikipedia"),
    ("whatsapp", "whatsapp"),
    ("netflix", "netflix"),
    ("spotify", "spotify"),
    ("chatgpt", "chatgpt"),
    ("chat gpt", "chatgpt"),
    ("gmail", "gmail"),
    ("amazon", "amazon"),
    ("linkedin", "linkedin"),
    ("twitter", "twitter"),
    (" x ", "twitter"),
    ("claude", "claude"),
    ("google", "google"),
)

# Sites that accept a search URL and can be searched "in place".
SEARCHABLE_SITES = frozenset(
    {"youtube", "github", "reddit", "wikipedia", "google", "amazon", "spotify", "gmail"}
)

_WINDOW_RUNNER: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
_WMCTRL_TIMEOUT_S = 0.2
_INTROSPECT_TIMEOUT_S = 0.35
_PROCESS_TIMEOUT_S = 0.2

_INTROSPECT_ENSURED = False
_ACCESSIBILITY_ENSURED = False
_TOOLKIT_ACCESSIBILITY_ENSURED = False

_GUI_PROCESS_PATTERN = (
    "firefox|brave|chrome|code|gnome-text-editor|nautilus|discord|spotify|lutris|vlc"
)
_GUI_PROCESS_TITLES: tuple[tuple[str, str], ...] = (
    ("firefox", "Mozilla Firefox"),
    ("brave", "Brave"),
    ("chrome", "Google Chrome"),
    ("code", "Visual Studio Code"),
    ("gnome-text-editor", "Text Editor"),
    ("nautilus", "Files"),
    ("discord", "Discord"),
    ("spotify", "Spotify"),
    ("lutris", "Lutris"),
    ("vlc", "VLC"),
)

_APP_ID_NAMES: tuple[tuple[str, str], ...] = (
    ("firefox", "firefox"),
    ("brave", "brave"),
    ("chrome", "chrome"),
    ("chromium", "chrome"),
    ("visual studio code", "code"),
    ("vscode", "code"),
    ("code", "code"),
    ("gnome-text-editor", "text editor"),
    ("texteditor", "text editor"),
    ("nautilus", "files"),
    ("discord", "discord"),
    ("spotify", "spotify"),
    ("lutris", "lutris"),
    ("vlc", "vlc"),
    ("ptyxis", "terminal"),
    ("gnome-terminal", "terminal"),
    ("gnome-calculator", "calculator"),
    ("gnome-control-center", "settings"),
)

_INTROSPECT_TITLE = re.compile(r"'title':\s*<'((?:\\'|[^'])*)'\s*>")
_INTROSPECT_APP_ID = re.compile(r"'app-id':\s*<'((?:\\'|[^'])*)'\s*>")
_INTROSPECT_WM_CLASS = re.compile(r"'wm-class':\s*<'((?:\\'|[^'])*)'\s*>")
_INTROSPECT_FOCUS = re.compile(r"'has-focus':\s*<(true|false)>")
_INTROSPECT_ENTRY = re.compile(r"\{[^{}]*\}")


@dataclass
class SessionContext:
    last_app: str = ""
    last_browser: str = ""
    last_query: str = ""
    last_site: str = ""
    active_site: str = ""
    focused_window: str = ""
    focused_app: str = ""
    last_reply: str = ""
    last_typed_text: str = ""
    last_action: str = ""
    open_windows: list[str] = field(default_factory=list)
    # Phase 8 eyes: the newest @ref snapshot (dicts produced by
    # ``assistant.vision.screen_watcher.UIElement.as_dict``).
    screen_elements: list[dict[str, Any]] = field(default_factory=list)
    screen_source: str = ""
    turns: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=5))

    def reset(self) -> None:
        self.last_app = ""
        self.last_browser = ""
        self.last_query = ""
        self.last_site = ""
        self.active_site = ""
        self.focused_window = ""
        self.focused_app = ""
        self.last_reply = ""
        self.last_typed_text = ""
        self.last_action = ""
        self.open_windows.clear()
        self.screen_elements.clear()
        self.screen_source = ""
        self.turns.clear()

    def note_app(self, app_name: str, *, browser: bool | None = None) -> None:
        cleaned = (app_name or "").strip().lower()
        if not cleaned:
            return
        self.last_app = cleaned
        if browser is None:
            browser = cleaned in BROWSERS
        if browser:
            self.last_browser = cleaned
        self.last_action = f"opened {cleaned}"

    def note_closed(self, app_name: str) -> None:
        cleaned = (app_name or "").strip().lower()
        if cleaned and self.last_app == cleaned:
            self.last_app = ""
        if cleaned:
            self.last_action = f"closed {cleaned}"

    def note_search(self, query: str, browser: str | None = None) -> None:
        self.last_query = (query or "").strip()
        if browser:
            self.last_browser = (browser or "").strip().lower()
        if self.last_query:
            self.last_action = f"searched {self.last_query}"

    def note_action(self, action: str) -> None:
        cleaned = re.sub(r"\s+", " ", action or "").strip()
        if cleaned:
            self.last_action = cleaned[:120]

    def note_screen(
        self,
        windows: list[str],
        *,
        focused_window: str = "",
        focused_app: str = "",
    ) -> None:
        """Update browser/site/focus from real window titles (newest window wins)."""
        self.open_windows = list(windows)[:20]
        browser = ""
        site = ""
        for title in reversed(self.open_windows):
            lowered = title.lower()
            if not browser:
                if "mozilla firefox" in lowered or lowered.endswith("firefox"):
                    browser = "firefox"
                elif "brave" in lowered:
                    browser = "brave"
                elif "google chrome" in lowered or "chromium" in lowered:
                    browser = "chrome"
            if not site:
                for keyword, key in SITE_KEYWORDS:
                    if keyword in lowered:
                        site = key
                        break
        if browser:
            self.last_browser = browser
        if site:
            self.active_site = site
            self.last_site = site
        elif browser and self.last_site:
            # The browser is still open but no site title is visible (for example a new
            # blank tab): keep the site the user was last working in.
            self.active_site = self.last_site
        if focused_window:
            self.focused_window = focused_window
        if focused_app:
            self.focused_app = focused_app

    def note_turn(self, user: str, assistant: str) -> None:
        self.turns.append(((user or "").strip(), (assistant or "").strip()))

    def recent_turns(self, limit: int = 4) -> list[tuple[str, str]]:
        return list(self.turns)[-max(1, int(limit)):]


SESSION = SessionContext()


def _run_quietly(
    argv: list[str],
    *,
    runner: Callable[..., Any] | None,
    timeout: float,
    guarded: bool = True,
) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only probe; never raise into a command."""
    if runner is None and guarded and os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    run = runner or _WINDOW_RUNNER
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # missing binary, timeout, non-zero exit
        logger.debug("Screen probe %s unavailable: %s", argv[0], type(exc).__name__)
        return None


def ensure_wayland_introspect(
    runner: Callable[..., Any] | None = None,
    session_type: str | None = None,
) -> bool:
    """Enable GNOME Shell's window introspection once per session on Wayland.

    ``gsettings set org.gnome.shell introspect true`` is written quietly to the user's
    dconf and makes ``org.gnome.Shell.Introspect.GetWindows`` return native windows. On
    X11 or any non-Wayland session this is a no-op. Tests always inject a runner.
    """
    global _INTROSPECT_ENSURED
    session = (
        session_type
        if session_type is not None
        else os.environ.get("XDG_SESSION_TYPE", "")
    ).strip().lower()
    if session != "wayland":
        return False
    if runner is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if _INTROSPECT_ENSURED and runner is None:
        return True
    result = _run_quietly(
        ["gsettings", "set", "org.gnome.shell", "introspect", "true"],
        runner=runner,
        timeout=1.0,
        guarded=False,
    )
    if runner is None:
        _INTROSPECT_ENSURED = True
    return bool(result is not None and getattr(result, "returncode", 1) == 0)


def ensure_accessibility(
    runner: Callable[..., Any] | None = None,
    session_type: str | None = None,
) -> dict[str, bool]:
    """Enable GNOME's AT-SPI toolkit bridge and shell introspection.

    ``toolkit-accessibility`` makes GTK, Qt and Electron apps publish their widgets on
    ``org.a11y.Bus``. GNOME 50.5 no longer ships the ``org.gnome.shell introspect`` key
    (verified on this machine: ``No such key``), so that call is attempted, logged quietly
    and reported honestly instead of pretending it succeeded. AT-SPI is the primary engine
    either way. Tests inject a runner and never touch dconf.
    """
    global _ACCESSIBILITY_ENSURED, _TOOLKIT_ACCESSIBILITY_ENSURED
    if runner is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return {"toolkit_accessibility": False, "introspect": False}
    if _ACCESSIBILITY_ENSURED and runner is None:
        return {"toolkit_accessibility": _TOOLKIT_ACCESSIBILITY_ENSURED, "introspect": _INTROSPECT_ENSURED}
    toolkit = _run_quietly(
        ["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"],
        runner=runner,
        timeout=1.0,
        guarded=False,
    )
    toolkit_ok = bool(toolkit is not None and getattr(toolkit, "returncode", 1) == 0)
    introspect_ok = ensure_wayland_introspect(runner=runner, session_type=session_type)
    if runner is None:
        _ACCESSIBILITY_ENSURED = True
        _TOOLKIT_ACCESSIBILITY_ENSURED = toolkit_ok
    logger.debug(
        "Accessibility: toolkit=%s introspect=%s",
        toolkit_ok,
        introspect_ok,
    )
    return {"toolkit_accessibility": toolkit_ok, "introspect": introspect_ok}


def parse_window_titles(output: str) -> list[str]:
    """Parse ``wmctrl -l`` lines: "0xID desktop host title" -> titles."""
    titles: list[str] = []
    for line in (output or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            title = parts[3].strip()
            if title:
                titles.append(title)
    return titles


def _gvariant_text(match: re.Match[str] | None) -> str:
    if match is None:
        return ""
    return match.group(1).replace("\\'", "'").strip()


def parse_introspect_windows(output: str) -> list[dict[str, Any]]:
    """Parse ``gdbus`` GetWindows GVariant output into plain dictionaries.

    The reply looks like ``([{'app-id': <'firefox'>, 'has-focus': <true>, ...}],)``.
    Entries without a title are still returned so the caller knows a native window exists.
    """
    windows: list[dict[str, Any]] = []
    for entry in _INTROSPECT_ENTRY.finditer(output or ""):
        block = entry.group(0)
        title = _gvariant_text(_INTROSPECT_TITLE.search(block))
        app_id = _gvariant_text(_INTROSPECT_APP_ID.search(block))
        wm_class = _gvariant_text(_INTROSPECT_WM_CLASS.search(block))
        focus_match = _INTROSPECT_FOCUS.search(block)
        has_focus = bool(focus_match and focus_match.group(1) == "true")
        if not (title or app_id or wm_class):
            continue
        windows.append(
            {
                "title": title,
                "app_id": app_id,
                "wm_class": wm_class,
                "has_focus": has_focus,
            }
        )
    return windows


def app_name_for_window(app_id: str, wm_class: str, title: str) -> str:
    """Map a native window's identifiers to the app key used by the tools."""
    text = f"{app_id} {wm_class} {title}".lower()
    for needle, name in _APP_ID_NAMES:
        if needle in text:
            return name
    return ""


def read_introspect_windows(
    runner: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Layer 1: ask GNOME Shell for every native Wayland window."""
    result = _run_quietly(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.Shell.Introspect",
            "--object-path",
            "/org/gnome/Shell/Introspect",
            "--method",
            "org.gnome.Shell.Introspect.GetWindows",
        ],
        runner=runner,
        timeout=_INTROSPECT_TIMEOUT_S,
    )
    if result is None or getattr(result, "returncode", 1) != 0:
        return []
    return parse_introspect_windows(getattr(result, "stdout", "") or "")


def read_window_titles(runner: Callable[..., Any] | None = None) -> list[str]:
    """Layer 2: ``wmctrl -l`` with a short timeout; catches XWayland windows only."""
    result = _run_quietly(["wmctrl", "-l"], runner=runner, timeout=_WMCTRL_TIMEOUT_S)
    if result is None or getattr(result, "returncode", 1) != 0:
        return []
    return parse_window_titles(getattr(result, "stdout", "") or "")


def read_gui_processes(runner: Callable[..., Any] | None = None) -> list[str]:
    """Layer 3: titles for running user GUI processes (headless-safe fallback)."""
    result = _run_quietly(
        ["pgrep", "-u", str(os.getuid()), "-a", _GUI_PROCESS_PATTERN],
        runner=runner,
        timeout=_PROCESS_TIMEOUT_S,
    )
    if result is None or getattr(result, "returncode", 1) != 0:
        return []
    output = (getattr(result, "stdout", "") or "").lower()
    titles: list[str] = []
    for needle, display in _GUI_PROCESS_TITLES:
        if needle in output:
            titles.append(display)
    return titles


def _display_name(key: str) -> str:
    return {
        "firefox": "Mozilla Firefox",
        "brave": "Brave",
        "chrome": "Google Chrome",
        "code": "Visual Studio Code",
        "text editor": "Text Editor",
        "files": "Files",
        "discord": "Discord",
        "spotify": "Spotify",
        "lutris": "Lutris",
        "vlc": "VLC",
        "youtube": "YouTube",
        "github": "GitHub",
        "google": "Google",
        "gmail": "Gmail",
        "reddit": "Reddit",
        "chatgpt": "ChatGPT",
        "whatsapp": "WhatsApp",
        "instagram": "Instagram",
        "netflix": "Netflix",
        "claude": "Claude",
    }.get((key or "").strip().lower(), (key or "").strip().title())


def _session_fallback_windows(process_runner: Callable[..., Any] | None) -> list[str]:
    """Never return an empty screen when Ultron itself just opened an app or site."""
    titles: list[str] = []
    for key in (SESSION.last_app, SESSION.last_browser, SESSION.last_site):
        if not key:
            continue
        display = _display_name(key)
        if display and display not in titles:
            titles.append(display)
    for title in read_gui_processes(process_runner):
        if title not in titles:
            titles.append(title)
    return titles


def refresh_screen_context(
    runner: Callable[..., Any] | None = None,
    *,
    introspect_runner: Callable[..., Any] | None = None,
    process_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Read the live screen with the 3-layer fallback and update SESSION.

    ``runner`` is the ``wmctrl`` runner (kept for the Phase 6 tests),
    ``introspect_runner`` is the ``gdbus`` runner, and ``process_runner`` is the
    ``pgrep`` runner. Production passes none of them and uses the real desktop.
    """
    ensure_wayland_introspect()

    native = read_introspect_windows(introspect_runner)
    titles = [
        str(item.get("title") or item.get("app_id") or item.get("wm_class") or "").strip()
        for item in native
        if str(item.get("title") or item.get("app_id") or item.get("wm_class") or "").strip()
    ]
    for title in read_window_titles(runner):
        if title not in titles:
            titles.append(title)

    focused_window = ""
    focused_app = ""
    for item in native:
        if item.get("has_focus"):
            focused_window = str(item.get("title") or "")
            focused_app = app_name_for_window(
                str(item.get("app_id") or ""),
                str(item.get("wm_class") or ""),
                focused_window,
            )
            break

    if native or titles:
        SESSION.note_screen(
            titles, focused_window=focused_window, focused_app=focused_app
        )
    else:
        fallback = _session_fallback_windows(process_runner)
        if fallback:
            SESSION.note_screen(fallback)
        if not SESSION.focused_window and SESSION.open_windows:
            SESSION.focused_window = SESSION.open_windows[0]
        if not SESSION.focused_app:
            SESSION.focused_app = SESSION.last_app or SESSION.last_browser

    return {
        "windows": list(SESSION.open_windows),
        "browser": SESSION.last_browser,
        "site": SESSION.active_site,
        "focused_window": SESSION.focused_window,
        "focused_app": SESSION.focused_app,
        "app": SESSION.last_app,
        "last_action": SESSION.last_action,
    }


def get_screen_context(runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Backward-compatible alias used by tools and the Phase 6 tests."""
    return refresh_screen_context(runner)


def screen_line(context: dict[str, Any] | None = None) -> str:
    """One compact line for diagnostics and legacy callers."""
    data = context or refresh_screen_context()
    windows = [re.sub(r"\s+", " ", title)[:45] for title in data.get("windows", [])[:4]]
    return (
        f"Screen: browser={data.get('browser') or 'unknown'}, "
        f"site={data.get('site') or 'unknown'}, "
        f"windows=[{'; '.join(windows)}]"
    )


def _visible_ui_tag(limit: int = 12, max_name: int = 28) -> str:
    """Compact ``@e1:button:"Show more"`` list from the watcher's latest snapshot."""
    role_display = {
        "push button": "button",
        "button": "button",
        "toggle button": "toggle",
        "switch": "toggle",
        "password text": "entry",
        "text": "text",
        "document web": "document",
        "document frame": "document",
    }
    shown: list[str] = []
    for item in SESSION.screen_elements:
        if len(shown) >= limit:
            break
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()[:max_name]
        role = role_display.get(str(item.get("role") or ""), str(item.get("role") or "item"))
        if not ref or (not name and not item.get("editable")):
            continue
        shown.append(f'{ref}:{role}:"{name}"' if name else f"{ref}:{role}")
    return ", ".join(shown)


def context_tag(context: dict[str, Any] | None = None, skills_hint: str = "") -> str:
    """Compact per-turn tag prepended to the user message (never to the static system)."""
    data = context or refresh_screen_context()

    def clean(value: Any, limit: int = 40) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:limit] or "none"

    parts = [
        f"browser={clean(data.get('browser'))}",
        f"site={clean(data.get('site'))}",
        f"app={clean(data.get('app'))}",
        f"focused={clean(data.get('focused_window'))}",
        f"last_action={clean(data.get('last_action'))}",
    ]
    tag = "[Context: " + ", ".join(parts)
    visible = _visible_ui_tag()
    if visible:
        tag += " | Visible UI: " + visible
    tag += "]"
    hint = re.sub(r"\s+", " ", (skills_hint or "")).strip()
    if hint:
        tag += f" [{hint[:180]}]"
    return tag


def active_search_site() -> str:
    """The site currently on screen when it supports an in-site search, else ''."""
    site = (SESSION.active_site or SESSION.last_site or "").strip().lower()
    return site if site in SEARCHABLE_SITES else ""
