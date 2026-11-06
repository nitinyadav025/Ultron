"""Desktop-native application launcher for Fedora 44 GNOME 50.5 Wayland."""

from __future__ import annotations

import difflib
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from assistant.brain.context import BROWSERS, SESSION
from assistant.config import PROJECT_ROOT
from assistant.tools.registry import tool

logger = logging.getLogger("ultron.apps")

# Spellings Whisper/user text really produced ("5ox", "file box") plus common names.
APP_ALIASES: dict[str, str] = {
    "fire fox": "firefox",
    "firefox": "firefox",
    "5ox": "firefox",
    "5x": "firefox",
    "file box": "firefox",
    "fire box": "firefox",
    "file manager": "files",
    "nautilus": "files",
    "texteditor": "text editor",
    "text editor": "text editor",
    "document editor": "text editor",
    "gedit": "text editor",
    "notepad": "text editor",
    "gnome text editor": "text editor",
    "vs code": "code",
    "vscode": "code",
    "visual studio": "code",
    "visual studio code": "code",
    "code": "code",
    "gnome terminal": "terminal",
    "console": "terminal",
    "shell": "terminal",
    "command prompt": "terminal",
    "command line": "terminal",
    "terminal": "terminal",
    "ptyxis": "terminal",
    "calc": "calculator",
    "calculator": "calculator",
    "control center": "settings",
    "gnome settings": "settings",
    "settings": "settings",
    "system settings": "settings",
    "wifi": "settings",
    "wi fi": "settings",
    "bluetooth": "settings",
    "sound settings": "settings",
    "display settings": "settings",
    "network settings": "settings",
    "privacy settings": "settings",
    "file browser": "files",
    "files": "files",
    "folder": "files",
    "downloads": "files",
    "documents": "files",
    "system monitor": "system monitor",
    "task manager": "system monitor",
    "processes": "system monitor",
    "software": "software",
    "app store": "software",
    "gnome software": "software",
    "store": "software",
    "gnome tweaks": "tweaks",
    "tweaks": "tweaks",
    "extensions": "extensions",
    "brave browser": "brave",
    "brave": "brave",
    "google chrome": "chrome",
    "chrome": "chrome",
    "spotify": "spotify",
    "vlc": "vlc",
    "lutris": "lutris",
    "games": "lutris",
    "loot risk": "lutris",
    "lootris": "lutris",
    "lu tris": "lutris",
    "loot res": "lutris",
    "web browser": "firefox",
    "browser": "firefox",
}

APP_DISPLAY: dict[str, str] = {
    "firefox": "Firefox",
    "files": "Files",
    "text editor": "Text Editor",
    "terminal": "Terminal",
    "calculator": "Calculator",
    "settings": "Settings",
    "code": "Code",
    "brave": "Brave",
    "chrome": "Chrome",
    "spotify": "Spotify",
    "vlc": "VLC",
    "lutris": "Lutris",
    "hermes": "Hermes",
    "software": "Software",
    "system monitor": "System Monitor",
    "tweaks": "Tweaks",
    "extensions": "Extensions",
    "discord": "Discord",
    "github": "GitHub",
    "youtube": "YouTube",
    "google": "Google",
    "gmail": "Gmail",
    "reddit": "Reddit",
    "chatgpt": "ChatGPT",
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
    "netflix": "Netflix",
    "claude": "Claude",
}

# Flatpak application IDs. ``flatpak kill <id>`` closes the whole sandbox cgroup; a raw
# ``pkill`` kills the bwrap/zypak stdio helper first and makes Electron's console.warn
# hit a broken pipe (the "write EPIPE" popup in the Nov 3 screenshot).
FLATPAK_APP_IDS: dict[str, tuple[str, ...]] = {
    "discord": ("com.discordapp.Discord",),
    "spotify": ("com.spotify.Client",),
    "steam": ("com.valvesoftware.Steam",),
    "brave": ("com.brave.Browser",),
    "chrome": ("com.google.Chrome",),
    "code": ("com.visualstudio.code",),
    "text editor": ("org.gnome.TextEditor",),
    "files": ("org.gnome.Nautilus",),
    "calculator": ("org.gnome.Calculator",),
    "settings": ("org.gnome.Settings",),
    "vlc": ("org.videolan.VLC",),
}

# Browser-only destinations: when one of these is not running as a native app and a
# browser is open, "close GitHub" means "close that tab", not "find an app".
WEB_ONLY_SITES = frozenset(
    {
        "github",
        "youtube",
        "google",
        "gmail",
        "reddit",
        "chatgpt",
        "whatsapp",
        "instagram",
        "netflix",
        "claude",
    }
)
_TAB_WORDS = frozenset({"tab", "this tab", "that tab", "current tab", "the tab"})

# Popular web apps with no local .desktop entry on this machine. They are opened in the
# session browser, never passed to xdg-open. Local installs always win (checked first).
WEB_APPS: dict[str, tuple[str, str]] = {
    "discord": ("Discord", "https://discord.com/app"),
    "instagram": ("Instagram", "https://www.instagram.com"),
    "youtube": ("YouTube", "https://www.youtube.com"),
    "whatsapp": ("WhatsApp", "https://web.whatsapp.com"),
    "chatgpt": ("ChatGPT", "https://chatgpt.com"),
    "gmail": ("Gmail", "https://mail.google.com"),
    "github": ("GitHub", "https://github.com"),
    "steam": ("Steam", "https://store.steampowered.com"),
    "netflix": ("Netflix", "https://www.netflix.com"),
    "spotify": ("Spotify", "https://open.spotify.com"),
    "reddit": ("Reddit", "https://www.reddit.com"),
    "twitter": ("Twitter", "https://x.com"),
    "x": ("X", "https://x.com"),
    "google": ("Google", "https://www.google.com"),
    "claude": ("Claude", "https://claude.ai"),
}
WEB_APP_ALIASES: dict[str, str] = {
    "whats app": "whatsapp",
    "you tube": "youtube",
    "git hub": "github",
    "chat gpt": "chatgpt",
    "insta": "instagram",
    "google mail": "gmail",
    "x twitter": "twitter",
}


def web_app_for(app_name: str) -> tuple[str, str] | None:
    """Return (display, URL) for a known web app, or None."""
    key = normalise_app_name(app_name)
    key = WEB_APP_ALIASES.get(key, key)
    return WEB_APPS.get(key)

# Safe-to-close user GUI apps only. System/session processes are absent by design, and
# an explicit deny list refuses them even if a name is ever added by mistake.
CLOSE_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "firefox": ("firefox", "firefox-bin"),
    "brave": ("brave", "brave-browser"),
    "chrome": ("chrome", "google-chrome", "google-chrome-stable"),
    "code": ("code", "code-oss"),
    "files": ("nautilus",),
    "nautilus": ("nautilus",),
    "vlc": ("vlc",),
    "spotify": ("spotify",),
    "calculator": ("gnome-calculator",),
    "text editor": ("gnome-text-editor",),
    "terminal": ("ptyxis", "gnome-terminal"),
    "settings": ("gnome-control-center",),
    "hermes": ("hermes", "hermes-desktop"),
}
NEVER_CLOSE = frozenset(
    {
        "gnome-shell",
        "systemd",
        "pipewire",
        "wireplumber",
        "xwayland",
        "bash",
        "sh",
        "python",
        "python3",
        "sshd",
        "wayland",
        "dbus-daemon",
        "gdm",
        "gdm3",
    }
)

# User entries win over system entries with the same desktop file name.
DESKTOP_DIRS = (
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
)

# Chromium-family browsers only expose web-page buttons/links to AT-SPI when renderer
# accessibility is forced at launch. gio launch cannot pass arguments, so these two prefer
# their executable (or a Flatpak run with the flag) when a URL is not involved.
CHROMIUM_BROWSERS = frozenset({"brave", "chrome"})
CHROMIUM_A11Y_FLAG = "--force-renderer-accessibility"


@dataclass(frozen=True)
class DesktopApp:
    key: str
    desktop_id: str
    desktop_path: str
    name: str
    generic: str
    exec_argv: tuple[str, ...]
    try_exec: str
    no_display: bool
    source: str


@dataclass(frozen=True)
class AppMatch:
    key: str
    display: str
    desktop_id: str = ""
    desktop_path: str = ""
    exec_argv: tuple[str, ...] = ()
    try_exec: str = ""
    yaml_entry: dict[str, Any] | None = None
    source: str = ""
    process_names: tuple[str, ...] = ()


_APP_CACHE: list[DesktopApp] | None = None

ProcessRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_process_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=5)


# Tests replace this with a fake runner: pytest must never kill a real browser.
_process_runner: ProcessRunner = _default_process_runner


def _default_flatpak_runner(
    argv: list[str], timeout: float = 2.0
) -> subprocess.CompletedProcess[str]:
    # `flatpak ps` is a short-lived CLI command whose stdout IS the data we need. Capture
    # it: subprocess.run() drains the pipes before returning, so this cannot hit the
    # detached-GUI "write EPIPE" case that requires DEVNULL for launched apps.
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


# Separate runner so a fake pgrep/kill runner never intercepts Flatpak sandbox handling.
_flatpak_runner: Callable[..., subprocess.CompletedProcess[str]] = _default_flatpak_runner


def _flatpak_running_apps() -> set[str]:
    """Return the running Flatpak application IDs (empty when Flatpak is unavailable)."""
    runner = _flatpak_runner
    if runner is _default_flatpak_runner and os.environ.get("PYTEST_CURRENT_TEST"):
        return set()
    try:
        result = runner(["flatpak", "ps", "--columns=application"], timeout=0.5)
    except (OSError, subprocess.SubprocessError, TypeError):
        return set()
    if getattr(result, "returncode", 1) != 0:
        return set()
    return {
        line.strip()
        for line in (getattr(result, "stdout", "") or "").splitlines()
        if line.strip()
    }


def _flatpak_ids_for(key: str, match: AppMatch | None = None) -> tuple[str, ...]:
    """Every Flatpak ID that could belong to this spoken app name."""
    ids: list[str] = list(FLATPAK_APP_IDS.get(key, ()))
    if match is not None:
        entry = match.yaml_entry or {}
        explicit = str(entry.get("flatpak_id") or "").strip()
        if explicit:
            ids.append(explicit)
        source = (match.source or "").lower()
        desktop_id = (match.desktop_id or "").removesuffix(".desktop")
        if "flatpak" in source and "." in desktop_id:
            ids.append(desktop_id)
    return tuple(dict.fromkeys(item for item in ids if item))


def _flatpak_kill(app_id: str) -> bool:
    """Close the whole Flatpak sandbox in one go (no broken zypak pipe)."""
    try:
        result = _flatpak_runner(["flatpak", "kill", app_id], timeout=2.0)
    except (OSError, subprocess.SubprocessError, TypeError):
        return False
    return getattr(result, "returncode", 1) == 0


def _tab_site(target: str) -> str:
    cleaned = normalise_app_name(target)
    # "close Google Chrome" names a browser, not the Google site. An explicit "tab" word
    # still reaches Ctrl+W through the caller's explicit-tab flag.
    if re.search(r"\b(?:firefox|brave|chrome|browser)\b", cleaned):
        return ""
    for site in WEB_ONLY_SITES:
        if re.search(rf"\b{re.escape(site)}\b", cleaned):
            return site
    return ""


def _browser_session_open() -> bool:
    return bool(SESSION.last_browser or SESSION.active_site or SESSION.open_windows)


def normalise_app_name(app_name: str) -> str:
    """Lowercase, collapse spaces, drop a leading article and punctuation."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (app_name or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return re.sub(r"^(?:the|a|an) ", "", cleaned).strip()


def app_key(app_name: str) -> str:
    """Resolve a spoken app name to the catalog key (or the cleaned name)."""
    cleaned = normalise_app_name(app_name)
    mapped = APP_ALIASES.get(cleaned)
    if mapped:
        return mapped
    # User-taught aliases ("remember that loot risk means lutris") win over the catalog.
    try:
        from assistant.brain.skills import runtime_alias

        learned = runtime_alias(cleaned)
    except Exception:
        learned = ""
    return learned or cleaned


def _catalog() -> dict[str, Any]:
    apps_file = PROJECT_ROOT / "config" / "apps.yaml"
    if not apps_file.exists():
        return {}
    try:
        data = yaml.safe_load(apps_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    apps = data.get("apps", {}) if isinstance(data, dict) else {}
    return apps if isinstance(apps, dict) else {}


def reset_app_cache() -> None:
    """Forget the .desktop scan (tests and after a package install)."""
    global _APP_CACHE
    _APP_CACHE = None


def _clean_exec(exec_line: str) -> tuple[str, ...]:
    """Turn an Exec= line into a clean argv list, dropping %U/%f field codes."""
    try:
        tokens = shlex.split(exec_line or "")
    except ValueError:
        tokens = (exec_line or "").split()
    cleaned: list[str] = []
    for token in tokens:
        token = re.sub(r"%[a-zA-Z]", "", token).strip()
        if token and not token.startswith("%"):
            cleaned.append(token)
    if cleaned and cleaned[0] == "env":
        cleaned = cleaned[1:]
        while cleaned and "=" in cleaned[0] and "/" not in cleaned[0]:
            cleaned.pop(0)
    return tuple(cleaned)


def _parse_desktop_file(path: Path) -> DesktopApp | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    section = ""
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section != "[Desktop Entry]" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    if values.get("Type", "Application") != "Application":
        return None
    name = values.get("Name", "").strip()
    if not name:
        return None
    exec_argv = _clean_exec(values.get("Exec", ""))
    try_exec = values.get("TryExec", "").strip()
    if not exec_argv and not try_exec:
        return None
    return DesktopApp(
        key=normalise_app_name(path.stem),
        desktop_id=path.name,
        desktop_path=str(path),
        name=name,
        generic=values.get("GenericName", "").strip(),
        exec_argv=exec_argv,
        try_exec=try_exec,
        no_display=values.get("NoDisplay", "").strip().lower() == "true",
        source=str(path),
    )


def scan_desktop_apps() -> list[DesktopApp]:
    """Cached scan of every visible .desktop application for this user."""
    global _APP_CACHE
    if _APP_CACHE is not None:
        return _APP_CACHE
    seen: set[str] = set()
    found: list[DesktopApp] = []
    for directory in DESKTOP_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            if path.name in seen:
                continue
            seen.add(path.name)
            app = _parse_desktop_file(path)
            if app is not None:
                found.append(app)
    _APP_CACHE = found
    return found


_COMM_MAX = 15  # Linux TASK_COMM_LEN is 16 bytes including the trailing NUL.


def _process_names_for(match: AppMatch) -> tuple[str, ...]:
    if match.process_names:
        names = list(match.process_names)
    else:
        names = []
        entry = match.yaml_entry or {}
        for key in ("binary", "alt_binary"):
            binary = str(entry.get(key) or "")
            if binary:
                names.append(Path(binary).name)
        for raw in (match.try_exec, match.exec_argv[0] if match.exec_argv else ""):
            if raw:
                names.append(Path(raw).name)
        ignored = {"gtk-launch", "flatpak", "sh", "bash", "env", "gio", "gio-launch"}
        names = [name for name in names if name and name not in ignored]
    names = list(dict.fromkeys(names))
    # pgrep -x matches /proc/<pid>/comm, which Linux truncates to 15 characters. Without
    # the truncated alias, closing gnome-calculator / gnome-text-editor /
    # gnome-control-center reports "not running" and leaves the window open.
    for name in list(names):
        truncated = name[:_COMM_MAX]
        if len(name) > _COMM_MAX and truncated not in names:
            names.append(truncated)
    return tuple(names)


def _match_texts(app: DesktopApp) -> set[str]:
    texts = {
        normalise_app_name(app.key),
        normalise_app_name(app.name),
        normalise_app_name(app.generic),
        normalise_app_name(app.desktop_id.removesuffix(".desktop")),
    }
    for raw in (app.try_exec, app.exec_argv[0] if app.exec_argv else ""):
        if raw:
            texts.add(normalise_app_name(Path(raw).stem))
    return {text for text in texts if text}


def _soundex(word: str) -> str:
    """Small phonetic key so 'fierfox' still finds Firefox."""
    letters = re.sub(r"[^a-z]", "", word.lower())
    if not letters:
        return ""
    codes = {
        **dict.fromkeys("bfpv", "1"),
        **dict.fromkeys("cgjkqsxz", "2"),
        **dict.fromkeys("dt", "3"),
        "l": "4",
        **dict.fromkeys("mn", "5"),
        "r": "6",
    }
    first = letters[0].upper()
    last = codes.get(letters[0], "")
    digits: list[str] = []
    for char in letters[1:]:
        code = codes.get(char, "")
        if code and code != last:
            digits.append(code)
        if char not in "hw":
            last = code
    return (first + "".join(digits) + "000")[:4]


def _score_app(query: str, app: DesktopApp) -> float:
    texts = _match_texts(app)
    if query in texts:
        return 1.0
    query_tokens = query.split()
    best = 0.0
    for text in texts:
        if len(query_tokens) >= 2 and all(token in text.split() for token in query_tokens):
            best = max(best, 0.92)
        best = max(best, difflib.SequenceMatcher(None, query, text).ratio())
        if " " not in query and len(query) >= 4 and _soundex(query):
            if any(_soundex(word) == _soundex(query) for word in text.split() if len(word) >= 4):
                best = max(best, 0.87)
    if app.no_display:
        best -= 0.08
    return best


def _desktop_path_for_id(desktop_id: str) -> str:
    """Full path of a .desktop file from any indexed application directory."""
    cleaned = (desktop_id or "").strip()
    if not cleaned:
        return ""
    if not cleaned.endswith(".desktop"):
        cleaned += ".desktop"
    for directory in DESKTOP_DIRS:
        candidate = directory / cleaned
        if candidate.is_file():
            return str(candidate)
    return ""


def find_app(app_name: str) -> AppMatch | None:
    """Resolve a spoken name against apps.yaml (high priority) and .desktop files."""
    cleaned = normalise_app_name(app_name)
    if not cleaned:
        return None
    key = app_key(app_name)
    catalog = _catalog()
    entry = catalog.get(key)
    if isinstance(entry, dict):
        display = APP_DISPLAY.get(key, str(entry.get("name") or key.title()))
        desktop_id = str(entry.get("desktop_id") or "")
        desktop_path = _desktop_path_for_id(desktop_id)
        return AppMatch(
            key=key,
            display=display,
            desktop_id=desktop_id,
            desktop_path=desktop_path,
            yaml_entry=entry,
            source="apps.yaml",
            process_names=CLOSE_EXECUTABLES.get(key, ()),
        )

    best: tuple[float, DesktopApp] | None = None
    # An alias ("app store" -> "software") must scan the resolved catalog key, not the
    # spoken words, or the fuzzy match never finds the real desktop entry.
    scan_query = normalise_app_name(key) or cleaned
    for app in scan_desktop_apps():
        score = _score_app(scan_query, app)
        if best is None or score > best[0]:
            best = (score, app)
    if best is None or best[0] < 0.86:
        return None
    app = best[1]
    return AppMatch(
        key=key or normalise_app_name(app.key),
        display=app.name,
        desktop_id=app.desktop_id,
        desktop_path=app.desktop_path,
        exec_argv=app.exec_argv,
        try_exec=app.try_exec,
        source=app.source,
        process_names=_process_names_for(
            AppMatch(
                key=app.key,
                display=app.name,
                desktop_id=app.desktop_id,
                desktop_path=app.desktop_path,
                exec_argv=app.exec_argv,
                try_exec=app.try_exec,
                source=app.source,
            )
        ),
    )


def _flatpak_id_for_match(match: AppMatch) -> str:
    entry = match.yaml_entry or {}
    explicit = str(entry.get("flatpak_id") or "").strip()
    if explicit:
        return explicit
    desktop_id = (match.desktop_id or "").removesuffix(".desktop")
    if "flatpak" in (match.source or "").lower() and "." in desktop_id:
        return desktop_id
    return ""


def _direct_executable(match: AppMatch) -> list[str] | None:
    entry = match.yaml_entry or {}
    candidates: list[str] = []
    for key in ("binary", "alt_binary"):
        binary = str(entry.get(key) or "").strip()
        if binary:
            candidates.append(binary)
    if not candidates and match.try_exec:
        candidates.append(match.try_exec)
    if not candidates and match.exec_argv:
        candidates.append(match.exec_argv[0])
    for binary in candidates:
        if shutil.which(binary):
            return [binary]
    return None


def _launch_command_for(match: AppMatch) -> tuple[list[str], str] | None:
    """GNOME-native launch order: gio launch, gtk-launch, flatpak run, then Exec."""
    flatpak_id = _flatpak_id_for_match(match)

    # Chromium-family browsers need the renderer accessibility flag, which gio launch
    # cannot pass. Prefer the executable (or Flatpak with the flag) for these two only.
    if match.key in CHROMIUM_BROWSERS:
        direct = _direct_executable(match)
        if direct:
            return [*direct, CHROMIUM_A11Y_FLAG], Path(direct[0]).name
        if flatpak_id and shutil.which("flatpak"):
            return ["flatpak", "run", flatpak_id, CHROMIUM_A11Y_FLAG], flatpak_id

    desktop_path = match.desktop_path or _desktop_path_for_id(match.desktop_id)
    if desktop_path and shutil.which("gio"):
        return ["gio", "launch", desktop_path], match.desktop_id or Path(desktop_path).name
    # gtk-launch needs the .desktop file to exist; without it, go straight to Flatpak.
    if match.desktop_id and desktop_path and shutil.which("gtk-launch"):
        return ["gtk-launch", match.desktop_id], match.desktop_id
    if flatpak_id and shutil.which("flatpak"):
        return ["flatpak", "run", flatpak_id], flatpak_id
    direct = _direct_executable(match)
    if direct:
        return direct, Path(direct[0]).name
    return None


def is_known_app(app_name: str) -> bool:
    """True for apps.yaml entries and installed .desktop applications."""
    return find_app(app_name) is not None


def resolve_known_app(app_name: str) -> tuple[list[str], str] | None:
    """Launch command for a known app, or None. Unknown names are never resolved."""
    match = find_app(app_name)
    if match is None:
        return None
    return _launch_command_for(match)


def resolve_app_launch_command(app_name: str) -> tuple[list[str], str]:
    """Return an EXECUTABLE argv that accepts a URL/file argument.

    ``gio launch`` is perfect for opening an app, but it cannot receive a URL. The web
    tools therefore use this resolver, which returns ``["firefox"]`` or
    ``["brave-browser", "--force-renderer-accessibility"]`` instead.
    """
    cleaned = app_name.strip().lower()
    match = find_app(app_name)
    if match is not None:
        direct = _direct_executable(match)
        if direct:
            if match.key in CHROMIUM_BROWSERS:
                return [*direct, CHROMIUM_A11Y_FLAG], Path(direct[0]).name
            return direct, Path(direct[0]).name
        flatpak_id = _flatpak_id_for_match(match)
        if flatpak_id and shutil.which("flatpak"):
            if match.key in CHROMIUM_BROWSERS:
                return ["flatpak", "run", flatpak_id, CHROMIUM_A11Y_FLAG], flatpak_id
            return ["flatpak", "run", flatpak_id], flatpak_id

    if cleaned.endswith(".desktop"):
        desktop_path = _desktop_path_for_id(cleaned)
        if desktop_path:
            try:
                argv = _parse_desktop_file(Path(desktop_path))
            except Exception:
                argv = None
            if argv is not None and argv.exec_argv and shutil.which(argv.exec_argv[0]):
                return list(argv.exec_argv), Path(argv.exec_argv[0]).name
    if shutil.which(cleaned):
        return [cleaned], cleaned
    return [cleaned], cleaned


def focus_target_window(app_name: str, *, try_wmctrl: bool = True) -> dict[str, Any]:
    """Raise an existing window on GNOME Wayland.

    ``wmctrl -a`` only sees XWayland windows. When it fails (a native Wayland window),
    relaunching the .desktop entry through ``gio launch`` makes GNOME activate and raise
    the single-instance window instead of opening a second copy.
    """
    match = find_app(app_name)
    if match is None:
        return {
            "ok": False,
            "unknown": True,
            "app_name": normalise_app_name(app_name),
            "error": f"I couldn't find an app named {normalise_app_name(app_name)}.",
        }
    if try_wmctrl and shutil.which("wmctrl"):
        try:
            moved = _process_runner(["wmctrl", "-a", match.display])
            if moved.returncode == 0:
                SESSION.note_app(match.key)
                return {
                    "ok": True,
                    "app_name": match.display,
                    "focused": True,
                    "method": "wmctrl",
                }
        except (OSError, subprocess.SubprocessError):
            pass
    payload, command = _launch_match(match)
    if not payload.get("ok"):
        return {**payload, "focused": False}
    return {
        "ok": True,
        "app_name": match.display,
        "focused": True,
        "method": "gio_launch",
        "command": command,
    }


def _launch_match(match: AppMatch) -> tuple[dict[str, Any], list[str]]:
    """Launch a resolved app; return (result_payload_or_empty, command)."""
    resolved_command = _launch_command_for(match)
    if resolved_command is None:
        return (
            {
                "ok": False,
                "app_name": match.display,
                "error": f"{match.display} is not installed.",
            },
            [],
        )
    cmd, resolved = resolved_command
    try:
        # NEVER give a detached GUI/Electron/Flatpak process a pipe. When the Popen handle
        # is collected, Electron's console.warn writes into a closed pipe and throws
        # "Error: write EPIPE" (the Flatpak Discord popup at 19:20:19). All three streams
        # go to the null device and the child owns a new session.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        poll = getattr(proc, "poll", None)
        returncode = poll() if callable(poll) else None
        if returncode not in (None, 0):
            return (
                {
                    "ok": False,
                    "app_name": match.display,
                    "command": cmd,
                    "error": f"{match.display} exited immediately ({returncode}).",
                },
                cmd,
            )
        SESSION.note_app(match.key)
        if match.key in BROWSERS:
            from assistant.speech.mic_input import schedule_mic_volume_restore

            schedule_mic_volume_restore()
        return (
            {
                "ok": True,
                "app_name": match.display,
                "resolved": resolved,
                "command": cmd,
                "pid": proc.pid,
            },
            cmd,
        )
    except Exception as exc:
        return (
            {"ok": False, "app_name": match.display, "command": cmd, "error": str(exc)},
            cmd,
        )


@tool(risk_level="safe", read_only=False)
def open_app(app_name: str) -> dict[str, Any]:
    """Open a desktop app by name."""
    cleaned = normalise_app_name(app_name)
    match = find_app(app_name)
    if match is None:
        web = web_app_for(app_name)
        if web is not None:
            display, url = web
            # Lazy import: web.py imports this module, so a top-level import would cycle.
            from assistant.tools.web import choose_browser, open_url

            browser = choose_browser(None)
            opened = open_url(url, browser or None)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "web_app": True,
                    "app_name": display,
                    "url": url,
                    "error": opened.get("error") or f"I couldn't open {display}.",
                }
            SESSION.note_app(browser or display, browser=bool(browser))
            where = browser.title() if browser else "your browser"
            return {
                "ok": True,
                "web_app": True,
                "app_name": display,
                "browser": browser,
                "url": url,
                "reply": f"I've opened {display} in {where}.",
            }
        return {
            "ok": False,
            "unknown": True,
            "app_name": cleaned,
            "error": f"I couldn't find an app named {cleaned}.",
        }
    payload, _cmd = _launch_match(match)
    return payload


def _kill_argv(pids: set[int]) -> list[str]:
    """SIGTERM the main process group when possible, else the individual PIDs."""
    groups: set[int] = set()
    for pid in pids:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            continue
        if pgid == pid and pgid not in (0, 1, os.getpgrp()):
            groups.add(pgid)
    if groups:
        return ["kill", "-TERM", *[f"-{group}" for group in sorted(groups)]]
    return ["kill", "-TERM", *[str(pid) for pid in sorted(pids)]]


@tool(risk_level="safe", read_only=False)
def close_app(app_name: str) -> dict[str, Any]:
    """Close a running desktop app or a browser tab."""
    cleaned = normalise_app_name(app_name)
    key = app_key(app_name)
    tab_site = _tab_site(cleaned)
    explicit_tab = (
        cleaned in _TAB_WORDS
        or cleaned.endswith(" tab")
        or " tab " in f" {cleaned} "
    )

    # A Flatpak Electron app is closed through ``flatpak kill``: killing bwrap/zypak with
    # pkill breaks the sandbox's stdio pipes and Electron shows a "write EPIPE" dialog.
    running_flatpak = _flatpak_running_apps() if _flatpak_ids_for(key) else set()
    native_flatpak = ""
    for app_id in _flatpak_ids_for(key):
        if app_id in running_flatpak:
            native_flatpak = app_id
            break

    # "close GitHub" / "close this GitHub tab" while a browser is open means Ctrl+W.
    if (explicit_tab or tab_site) and not native_flatpak and _browser_session_open():
        from assistant.tools.desktop_control import browser_tab_control

        result = browser_tab_control("close_tab")
        display = APP_DISPLAY.get(tab_site, tab_site.title()) if tab_site else "that"
        if result.get("ok"):
            SESSION.note_action(f"closed {display} tab")
            return {
                "ok": True,
                "tab_closed": True,
                "app_name": display,
                "running": True,
                "closed": True,
                "action": "close_tab",
            }
        return {
            "ok": False,
            "tab_closed": True,
            "app_name": display,
            "error": result.get("error") or "I couldn't close that tab.",
        }

    if native_flatpak:
        if _flatpak_kill(native_flatpak):
            display = APP_DISPLAY.get(key, key.title())
            SESSION.note_closed(key)
            logger.info("Closed Flatpak %s", native_flatpak)
            return {
                "ok": True,
                "running": True,
                "closed": True,
                "app_name": display,
                "flatpak": native_flatpak,
            }
        return {
            "ok": False,
            "app_name": APP_DISPLAY.get(key, key.title()),
            "error": f"I couldn't close the Flatpak {native_flatpak}.",
        }

    match = find_app(app_name)
    if match is None:
        return {
            "ok": False,
            "unknown": True,
            "app_name": cleaned,
            "error": f"I couldn't find an app named {cleaned}.",
        }
    key, display = match.key, match.display

    flatpak_ids = _flatpak_ids_for(key, match)
    if flatpak_ids and not running_flatpak:
        running_flatpak = _flatpak_running_apps()
    for app_id in flatpak_ids:
        if app_id in running_flatpak:
            if _flatpak_kill(app_id):
                SESSION.note_closed(key)
                return {
                    "ok": True,
                    "running": True,
                    "closed": True,
                    "app_name": display,
                    "flatpak": app_id,
                }
            return {
                "ok": False,
                "app_name": display,
                "error": f"I couldn't close the Flatpak {app_id}.",
            }

    executables = _process_names_for(match)
    if cleaned in NEVER_CLOSE or key in NEVER_CLOSE or any(name in NEVER_CLOSE for name in executables):
        return {
            "ok": False,
            "unknown": True,
            "app_name": cleaned,
            "error": "Refusing to close a system process.",
        }

    # XWayland windows can be asked to close politely first.
    if shutil.which("wmctrl") and any(
        display.lower() in title.lower() for title in SESSION.open_windows
    ):
        try:
            _process_runner(["wmctrl", "-c", display])
        except (OSError, subprocess.SubprocessError):
            pass

    if not executables:
        # No process name (for example a DBus-only desktop app): close by window title.
        if shutil.which("wmctrl"):
            try:
                closed = _process_runner(["wmctrl", "-c", display])
            except (OSError, subprocess.SubprocessError) as exc:
                return {"ok": False, "app_name": display, "error": str(exc)}
            if closed.returncode == 0:
                SESSION.note_closed(key)
                return {"ok": True, "running": True, "closed": True, "app_name": display}
        return {"ok": True, "running": False, "closed": False, "app_name": display}
    if shutil.which("pgrep") is None:
        return {"ok": False, "app_name": display, "error": "pgrep is not installed."}

    pids: set[int] = set()
    try:
        for executable in executables:
            found = _process_runner(["pgrep", "-x", executable])
            if found.returncode == 0:
                for token in (found.stdout or "").split():
                    if token.isdigit():
                        pids.add(int(token))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "app_name": display, "error": f"Could not look for {display}: {exc}"}

    pids.discard(os.getpid())
    pids.discard(1)
    if not pids:
        return {"ok": True, "running": False, "closed": False, "app_name": display}

    try:
        killed = _process_runner(_kill_argv(pids))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "app_name": display, "error": f"Could not close {display}: {exc}"}
    if killed.returncode != 0:
        return {
            "ok": False,
            "app_name": display,
            "error": (killed.stderr or "kill failed").strip(),
        }
    logger.info("Closed %s (pids %s)", display, sorted(pids))
    SESSION.note_closed(key)
    return {"ok": True, "running": True, "closed": True, "app_name": display, "pids": sorted(pids)}


@tool(risk_level="safe", read_only=False)
def switch_app(app_name: str) -> dict[str, Any]:
    """Switch to a running desktop app, or open it if it is not running."""
    cleaned = normalise_app_name(app_name)
    match = find_app(app_name)
    if match is None:
        return {
            "ok": False,
            "unknown": True,
            "app_name": cleaned,
            "error": f"I couldn't find an app named {cleaned}.",
        }
    display = match.display
    process_names = _process_names_for(match)
    running = False
    if process_names and shutil.which("pgrep"):
        for executable in process_names:
            found = _process_runner(["pgrep", "-x", executable])
            if found.returncode == 0 and (found.stdout or "").strip():
                running = True
                break

    focused = False
    if shutil.which("wmctrl"):
        try:
            moved = _process_runner(["wmctrl", "-a", display])
            focused = moved.returncode == 0
        except (OSError, subprocess.SubprocessError):
            focused = False

    if not focused:
        payload, _cmd = _launch_match(match)
        if not payload.get("ok"):
            return {**payload, "running": running, "focused": False, "opened": False}
    SESSION.note_app(match.key)
    return {
        "ok": True,
        "app_name": display,
        "running": running,
        "focused": focused or running,
        "opened": not running,
    }
