"""Desktop-native application launcher for Fedora 44 GNOME 50.5 Wayland."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

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
    "vs code": "code",
    "vscode": "code",
    "code": "code",
    "gnome terminal": "terminal",
    "terminal": "terminal",
    "calc": "calculator",
    "calculator": "calculator",
    "brave browser": "brave",
    "brave": "brave",
    "google chrome": "chrome",
    "chrome": "chrome",
    "spotify": "spotify",
    "vlc": "vlc",
    "settings": "settings",
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
}

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
        "dbus-daemon",
        "gdm",
        "gdm3",
    }
)

ProcessRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_process_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=5)


# Tests replace this with a fake runner: pytest must never kill a real browser.
_process_runner: ProcessRunner = _default_process_runner


def normalise_app_name(app_name: str) -> str:
    """Lowercase, collapse spaces, drop a leading article and punctuation."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (app_name or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return re.sub(r"^(?:the|a|an) ", "", cleaned).strip()


def app_key(app_name: str) -> str:
    """Resolve a spoken app name to the catalog key (or the cleaned name)."""
    cleaned = normalise_app_name(app_name)
    return APP_ALIASES.get(cleaned, cleaned)


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


def is_known_app(app_name: str) -> bool:
    """True only for a configured app (plus its known aliases), never a guess."""
    return app_key(app_name) in _catalog()


def resolve_known_app(app_name: str) -> tuple[list[str], str] | None:
    """Launch command for a configured app, or None. Unknown names are never resolved."""
    entry = _catalog().get(app_key(app_name))
    if not isinstance(entry, dict):
        return None
    for key in ("binary", "alt_binary"):
        binary = str(entry.get(key) or "")
        if binary and shutil.which(binary):
            return [binary], binary
    desktop_id = str(entry.get("desktop_id") or "")
    if desktop_id and shutil.which("gtk-launch"):
        return ["gtk-launch", desktop_id], desktop_id
    flatpak_id = str(entry.get("flatpak_id") or "")
    if flatpak_id and shutil.which("flatpak"):
        return ["flatpak", "run", flatpak_id], flatpak_id
    return None


def resolve_app_launch_command(app_name: str) -> tuple[list[str], str]:
    """Resolve a human app name (e.g. 'Text Editor') to its Fedora launch command."""
    cleaned = app_name.strip().lower()
    apps_file = PROJECT_ROOT / "config" / "apps.yaml"
    catalog = (yaml.safe_load(apps_file.read_text(encoding="utf-8")) or {}).get("apps", {}) if apps_file.exists() else {}

    entry = catalog.get(cleaned) or next((v for k, v in catalog.items() if cleaned in k or k in cleaned), None)
    if entry:
        for key in ("binary", "alt_binary"):
            b = entry.get(key, "")
            if b and shutil.which(b):
                return [b], b
        desktop_id = entry.get("desktop_id", "")
        if desktop_id and shutil.which("gtk-launch"):
            return ["gtk-launch", desktop_id], desktop_id
        flatpak_id = entry.get("flatpak_id", "")
        if flatpak_id and shutil.which("flatpak"):
            return ["flatpak", "run", flatpak_id], flatpak_id

    if cleaned.endswith(".desktop") and shutil.which("gtk-launch"):
        return ["gtk-launch", app_name.strip()], app_name.strip()
    if shutil.which(cleaned):
        return [cleaned], cleaned

    for ddir in (Path("/usr/share/applications"), Path("/var/lib/flatpak/exports/share/applications"), Path.home() / ".local/share/applications"):
        if ddir.exists():
            for dfile in ddir.glob("*.desktop"):
                if cleaned.replace(" ", "") in dfile.stem.lower().replace("-", ""):
                    if shutil.which("gtk-launch"):
                        return ["gtk-launch", dfile.name], dfile.name
                    if shutil.which("gio"):
                        return ["gio", "launch", str(dfile)], dfile.name
    return [cleaned], cleaned


@tool(risk_level="safe", read_only=False)
def open_app(app_name: str) -> dict[str, Any]:
    """Open a desktop app by name."""
    cleaned = normalise_app_name(app_name)
    display = APP_DISPLAY.get(app_key(app_name), cleaned)
    if not is_known_app(app_name):
        # Never claim "X is open" for a name that is not in config/apps.yaml.
        return {
            "ok": False,
            "unknown": True,
            "app_name": cleaned,
            "error": f"Unknown app '{cleaned}'.",
        }
    resolved_command = resolve_known_app(app_name)
    if resolved_command is None:
        return {
            "ok": False,
            "app_name": display,
            "error": f"{display} is configured but not installed.",
        }
    cmd, resolved = resolved_command
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        try:
            _, err = proc.communicate(timeout=0.4)
            if proc.returncode not in (0, None):
                return {"ok": False, "app_name": display, "command": cmd, "error": err.decode("utf-8", errors="replace").strip()}
        except subprocess.TimeoutExpired:
            pass
        return {"ok": True, "app_name": display, "resolved": resolved, "command": cmd, "pid": proc.pid}
    except Exception as exc:
        return {"ok": False, "app_name": display, "command": cmd, "error": str(exc)}


@tool(risk_level="safe", read_only=False)
def close_app(app_name: str) -> dict[str, Any]:
    """Close a running desktop app."""
    cleaned = normalise_app_name(app_name)
    key = app_key(app_name)
    display = APP_DISPLAY.get(key, cleaned.title() or "That app")
    if cleaned in NEVER_CLOSE or key in NEVER_CLOSE:
        return {
            "ok": False,
            "unknown": True,
            "app_name": cleaned,
            "error": "Refusing to close a system process.",
        }
    executables = CLOSE_EXECUTABLES.get(key)
    if not executables:
        return {
            "ok": False,
            "unknown": True,
            "app_name": cleaned,
            "error": f"Unknown app '{cleaned}'.",
        }
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
        killed = _process_runner(["kill", "-TERM", *[str(pid) for pid in sorted(pids)]])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "app_name": display, "error": f"Could not close {display}: {exc}"}
    if killed.returncode != 0:
        return {
            "ok": False,
            "app_name": display,
            "error": (killed.stderr or "kill failed").strip(),
        }
    logger.info("Closed %s (pids %s)", display, sorted(pids))
    return {"ok": True, "running": True, "closed": True, "app_name": display, "pids": sorted(pids)}
