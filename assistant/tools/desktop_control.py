"""Full desktop control for GNOME Wayland, using only argv lists.

Keyboard injection uses ydotool over a user-owned ``ydotoold`` socket (the system
service is inactive here). If injection is unavailable, typing falls back to the
Wayland clipboard and says so; every other action reports an honest failure instead of
pretending it happened. No shell is ever used.
"""

from __future__ import annotations

import atexit
import glob
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from assistant.brain.context import SESSION
from assistant.config import WORKSPACE_DIR
from assistant.tools.registry import tool

logger = logging.getLogger("ultron.desktop")

# Linux input-event keycodes (see /usr/include/linux/input-event-codes.h)
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_LEFTALT = 56
KEY_LEFTMETA = 125
KEY_TAB = 15
KEY_T = 20
KEY_W = 17
KEY_R = 19
KEY_V = 47
KEY_H = 35
KEY_F11 = 87
KEY_UP = 103
KEY_DOWN = 108
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_HOME = 102
KEY_END = 107
KEY_PAGEUP = 104
KEY_PAGEDOWN = 109
KEY_ENTER = 28
KEY_PLAYPAUSE = 164
KEY_NEXTSONG = 163
KEY_PREVIOUSSONG = 165

MAX_TYPE_CHARS = 200
ProcessRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_run(argv: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


# Tests replace these two runners: no real keystrokes or DBus calls during pytest.
_run: Callable[..., subprocess.CompletedProcess[str]] = _default_run
_ydotool_runner: Callable[[list[str]], tuple[bool, str]] | None = None

# One daemon for the whole chat/listen session. Starting it per command cost ~500 ms and
# the Nov 2 log showed scroll keys reaching the terminal in between.
ULTRON_YDOTOOL_SOCKET = "/tmp/.ultron_ydotool.sock"
_ydotoold_proc: subprocess.Popen[bytes] | None = None
_ydotool_socket = ""
_ydotoold_lock = threading.Lock()
_atexit_registered = False

# When Ultron runs inside a terminal, keyboard actions must go to the last app window.
_focus_context = False
_skip_focus_once = False
_focus_delay_s = 0.08


def _socket_path() -> str:
    explicit = os.environ.get("YDOTOOL_SOCKET", "").strip()
    if explicit:
        return explicit
    return ULTRON_YDOTOOL_SOCKET


def ensure_ydotool() -> str:
    """Start ONE user-level ydotoold for the session; return its socket (or '')."""
    global _ydotoold_proc, _ydotool_socket, _atexit_registered
    socket = _socket_path()
    with _ydotoold_lock:
        proc = _ydotoold_proc
        if proc is not None and proc.poll() is None and Path(socket).exists():
            _ydotool_socket = socket
            return socket
        if proc is not None and proc.poll() is not None:
            _ydotoold_proc = None
        if Path(socket).exists() and _ydotoold_proc is None:
            # A socket left by an earlier run: try it once; _ydotool replaces it if stale.
            _ydotool_socket = socket
            return socket
        if shutil.which("ydotoold") is None:
            return ""
        try:
            _ydotoold_proc = subprocess.Popen(
                ["ydotoold", "--socket-path", socket],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            _ydotoold_proc = None
            return ""
        if not _atexit_registered:
            atexit.register(stop_ydotool)
            _atexit_registered = True
        for _ in range(50):
            if Path(socket).exists():
                _ydotool_socket = socket
                logger.info("ydotoold ready at %s", socket)
                return socket
            time.sleep(0.02)
        stop_ydotool()
        return ""


def start_ydotool() -> str:
    """Public startup hook for chat/listen: start the one daemon early, not per command.

    Tests never start a real daemon (pytest sets PYTEST_CURRENT_TEST); the mocked
    ``_ydotool_runner`` path is used instead.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ULTRON_NO_YDOTOOL"):
        return ""
    return ensure_ydotool()


def stop_ydotool() -> None:
    """Stop only the daemon this process started (never the user's system service)."""
    global _ydotoold_proc
    proc = _ydotoold_proc
    _ydotoold_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    socket = _socket_path()
    if socket.startswith("/tmp/") and Path(socket).exists():
        try:
            Path(socket).unlink()
        except OSError:
            pass


def _ydotool_run(socket: str, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "YDOTOOL_SOCKET": socket}
    return subprocess.run(
        ["ydotool", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ydotool(args: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    """Run one ydotool command on the persistent socket; replace a stale one once."""
    if _ydotool_runner is not None:
        return _ydotool_runner(args)
    if shutil.which("ydotool") is None:
        return False, "ydotool is not installed."
    socket = ensure_ydotool()
    if not socket:
        return False, "The keyboard daemon could not start."
    try:
        result = _ydotool_run(socket, args, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "ydotool failed").strip()
        if "socket" in error.lower() or "connect" in error.lower():
            stop_ydotool()
            socket = ensure_ydotool()
            if socket:
                try:
                    result = _ydotool_run(socket, args, timeout)
                except (OSError, subprocess.SubprocessError) as exc:
                    return False, str(exc)
                if result.returncode == 0:
                    return True, ""
                error = (result.stderr or result.stdout or error).strip()
        return False, error
    return True, ""


# -- window focus before keyboard actions -----------------------------------------


def set_terminal_context(enabled: bool = True) -> None:
    """Enable pre-action window focus when Ultron runs inside a terminal."""
    global _focus_context
    _focus_context = bool(enabled)


def note_explicit_terminal() -> None:
    """The next action explicitly targets the terminal; do not move focus."""
    global _skip_focus_once
    _skip_focus_once = True


def _display_name(app: str) -> str:
    from assistant.tools.apps import APP_DISPLAY

    key = (app or "").strip().lower()
    return APP_DISPLAY.get(key, key.replace("_", " ").title())


def _focus_target() -> None:
    """Focus the last app/browser before scroll/tab/type so keys never hit the terminal."""
    global _skip_focus_once
    if _skip_focus_once:
        _skip_focus_once = False
        return
    if not _focus_context:
        return
    target = SESSION.last_browser or SESSION.last_app
    if not target or shutil.which("wmctrl") is None:
        return
    try:
        _run(["wmctrl", "-a", _display_name(target)], timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return
    if _focus_delay_s > 0:
        time.sleep(_focus_delay_s)


def _tap(code: int) -> list[str]:
    return [f"{code}:1", f"{code}:0"]


def _combo(codes: list[int]) -> list[str]:
    pressed = [f"{code}:1" for code in codes]
    released = [f"{code}:0" for code in reversed(codes)]
    return pressed + released


def _looks_secret(text: str) -> bool:
    lowered = (text or "").lower()
    needles = (".env", "password", "passwd", "api key", "api_key", "secret", "token")
    return any(needle in lowered for needle in needles)


def _copy_clipboard(text: str) -> bool:
    if shutil.which("wl-copy") is None:
        return False
    try:
        result = subprocess.run(
            ["wl-copy"],
            input=text,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@tool(risk_level="safe", read_only=False)
def type_text(text: str, press_enter: bool = False) -> dict[str, Any]:
    """Type the given text into the focused window."""
    cleaned = " ".join((text or "").replace("\r", " ").replace("\n", " ").split())
    if not cleaned:
        return {"ok": False, "error": "There was no text to type."}
    if len(cleaned) > MAX_TYPE_CHARS:
        return {"ok": False, "error": f"That text is longer than {MAX_TYPE_CHARS} characters."}
    if _looks_secret(cleaned):
        return {"ok": False, "error": "I won't type passwords, keys or tokens."}

    _focus_target()
    copied = _copy_clipboard(cleaned)
    # --key-delay 12 keeps a 200-character sentence well under 0.15 s instead of the
    # default per-character delay. The persistent daemon removes the ~500 ms spawn.
    ok, _err = _ydotool(["type", "--key-delay", "12", "--", cleaned])
    if ok:
        if press_enter:
            _ydotool(["key", *_tap(KEY_ENTER)])
        return {
            "ok": True,
            "method": "ydotool",
            "text_chars": len(cleaned),
            "clipboard": copied,
            "reply": "Typed that.",
        }

    if not copied:
        copied = _copy_clipboard(cleaned)
    if copied:
        pasted, _paste_err = _ydotool(["key", *_combo([KEY_LEFTCTRL, KEY_V])])
        if pasted:
            return {
                "ok": True,
                "method": "clipboard_paste",
                "text_chars": len(cleaned),
                "reply": "I pasted the text from the clipboard.",
            }
        return {
            "ok": True,
            "method": "clipboard",
            "text_chars": len(cleaned),
            "reply": "I copied the text to the clipboard. Press Ctrl+V to paste it.",
        }
    return {
        "ok": False,
        "error": "I couldn't type that: keyboard injection and the clipboard are unavailable.",
    }


# PageDown/PageUp, not five Down/Up arrows: one "scroll down" moves a full screen and
# never prints ^[[B five times into the terminal when focus is lost.
_SCROLL_KEYS = {
    "down": KEY_PAGEDOWN,
    "up": KEY_PAGEUP,
    "page_down": KEY_PAGEDOWN,
    "page_up": KEY_PAGEUP,
    "top": KEY_HOME,
    "bottom": KEY_END,
}


@tool(risk_level="safe", read_only=False)
def scroll_page(direction: str = "down", amount: int = 1) -> dict[str, Any]:
    """Scroll down, up, to the top, to the bottom, a page up or a page down."""
    choice = (direction or "down").strip().lower()
    choice = {
        "slide down": "down",
        "slide up": "up",
        "page down": "page_down",
        "page up": "page_up",
        "to the top": "top",
        "to the bottom": "bottom",
    }.get(choice, choice)
    if choice not in _SCROLL_KEYS:
        return {"ok": False, "error": f"I don't know how to scroll '{direction}'."}
    repeats = max(1, min(20, int(amount))) if choice in ("down", "up") else 1
    _focus_target()
    keys: list[str] = []
    for _ in range(repeats):
        keys.extend(_tap(_SCROLL_KEYS[choice]))
    ok, err = _ydotool(["key", *keys])
    if not ok:
        return {"ok": False, "error": f"I couldn't scroll: {err}"}
    return {"ok": True, "direction": choice, "amount": repeats}


_TAB_KEYS = {
    "new_tab": [KEY_LEFTCTRL, KEY_T],
    "close_tab": [KEY_LEFTCTRL, KEY_W],
    "next_tab": [KEY_LEFTCTRL, KEY_TAB],
    "prev_tab": [KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_TAB],
    "reopen_tab": [KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_T],
    "refresh": [KEY_LEFTCTRL, KEY_R],
    "back": [KEY_LEFTALT, KEY_LEFT],
    "forward": [KEY_LEFTALT, KEY_RIGHT],
}


@tool(risk_level="safe", read_only=False)
def browser_tab_control(action: str) -> dict[str, Any]:
    """Control browser tabs and navigation with standard shortcuts."""
    choice = (action or "").strip().lower()
    codes = _TAB_KEYS.get(choice)
    if codes is None:
        return {"ok": False, "error": f"I don't know the tab action '{action}'."}
    _focus_target()
    ok, err = _ydotool(["key", *_combo(codes)])
    if not ok:
        return {"ok": False, "error": f"I couldn't control the tab: {err}"}
    return {"ok": True, "action": choice}


_WINDOW_KEYS = {
    "next_window": [KEY_LEFTALT, KEY_TAB],
    "maximize": [KEY_LEFTMETA, KEY_UP],
    "minimize": [KEY_LEFTMETA, KEY_H],
    "fullscreen": [KEY_F11],
    "workspace_next": [KEY_LEFTCTRL, KEY_LEFTALT, KEY_RIGHT],
    "workspace_prev": [KEY_LEFTCTRL, KEY_LEFTALT, KEY_LEFT],
    "overview": [KEY_LEFTMETA],
}


@tool(risk_level="safe", read_only=False)
def window_control(action: str) -> dict[str, Any]:
    """Switch windows, maximize, minimize, fullscreen or change workspace."""
    choice = (action or "").strip().lower()
    codes = _WINDOW_KEYS.get(choice)
    if codes is None:
        return {"ok": False, "error": f"I don't know the window action '{action}'."}
    ok, err = _ydotool(["key", *_combo(codes)])
    if not ok:
        return {"ok": False, "error": f"I couldn't control the window: {err}"}
    return {"ok": True, "action": choice}


def _current_brightness() -> tuple[int, int] | None:
    for path in glob.glob("/sys/class/backlight/*/brightness"):
        try:
            current = int(Path(path).read_text(encoding="utf-8").strip())
            maximum = int((Path(path).parent / "max_brightness").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if maximum > 0:
            return current, maximum
    return None


def _gnome_brightness(percent: int) -> bool:
    if shutil.which("gdbus") is None:
        return False
    try:
        result = _run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.SettingsDaemon.Power",
                "--object-path",
                "/org/gnome/SettingsDaemon/Power",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Power.Screen",
                "Brightness",
                f"<int32 {percent}>",
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@tool(risk_level="safe", read_only=False)
def set_brightness(percent: int = -1, delta: int = 0) -> dict[str, Any]:
    """Set screen brightness to a percentage, or change it by a delta."""
    current = _current_brightness()
    current_percent = round(current[0] * 100 / current[1]) if current and current[1] else 50
    if percent >= 0:
        target = max(0, min(100, int(percent)))
    elif delta:
        target = max(0, min(100, current_percent + int(delta)))
    else:
        return {"ok": False, "error": "Say a brightness percentage or up/down."}

    if _gnome_brightness(target):
        return {"ok": True, "percent": target, "method": "gdbus"}
    if shutil.which("brightnessctl"):
        try:
            result = _run(["brightnessctl", "set", f"{target}%"])
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            return {"ok": True, "percent": target, "method": "brightnessctl"}
    if current:
        path = Path(glob.glob("/sys/class/backlight/*/brightness")[0])
        target_raw = round(target * current[1] / 100)
        try:
            path.write_text(str(target_raw), encoding="utf-8")
            return {"ok": True, "percent": target, "method": "sysfs"}
        except OSError:
            pass
    return {
        "ok": False,
        "percent": target,
        "error": "I couldn't change the screen brightness: no writable brightness control is available.",
    }


_MEDIA_KEYS = {
    "play_pause": KEY_PLAYPAUSE,
    "play": KEY_PLAYPAUSE,
    "pause": KEY_PLAYPAUSE,
    "next": KEY_NEXTSONG,
    "previous": KEY_PREVIOUSSONG,
}


@tool(risk_level="safe", read_only=False)
def media_control(action: str = "play_pause") -> dict[str, Any]:
    """Play, pause, or skip to the next/previous song."""
    choice = (action or "play_pause").strip().lower()
    code = _MEDIA_KEYS.get(choice)
    if code is None:
        return {"ok": False, "error": f"I don't know the media action '{action}'."}
    ok, err = _ydotool(["key", *_tap(code)])
    if not ok:
        return {"ok": False, "error": f"I couldn't control the media: {err}"}
    return {"ok": True, "action": choice}


def _battery_text() -> str:
    for path in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        base = Path(path)
        try:
            capacity = int((base / "capacity").read_text(encoding="utf-8").strip())
            status = (base / "status").read_text(encoding="utf-8").strip().lower()
        except (OSError, ValueError):
            continue
        if status in ("charging", "full"):
            return f"The battery is {capacity} percent and {status}."
        return f"The battery is {capacity} percent."
    return "This machine has no battery."


def _memory_text() -> str:
    try:
        data = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            data[key] = int(rest.strip().split()[0])
        total_gb = data.get("MemTotal", 0) / 1024 / 1024
        available_gb = data.get("MemAvailable", 0) / 1024 / 1024
        return f"{available_gb:.1f} GB of {total_gb:.1f} GB RAM is available."
    except (OSError, ValueError, IndexError):
        return "I couldn't read the memory usage."


@tool(risk_level="safe", read_only=True)
def system_info(kind: str = "status") -> dict[str, Any]:
    """Read the battery, the time/date, or the system status."""
    choice = (kind or "status").strip().lower()
    now = datetime.now()
    if choice == "battery":
        reply = _battery_text()
    elif choice in ("time", "clock"):
        reply = f"It is {now.strftime('%H:%M')}."
    elif choice in ("date", "today"):
        reply = f"Today is {now.strftime('%A, %B')} {now.day}, {now.year}."
    elif choice == "status":
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        reply = f"{_battery_text()} {_memory_text()} Load average {load:.2f}."
    else:
        return {"ok": False, "error": f"I don't know the system info '{kind}'."}
    return {"ok": True, "kind": choice, "reply": reply}


@tool(risk_level="safe", read_only=False)
def take_screenshot() -> dict[str, Any]:
    """Take a screenshot and save it in Pictures (or the workspace)."""
    pictures = Path.home() / "Pictures"
    directory = pictures if pictures.is_dir() else WORKSPACE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ultron_screenshot_{datetime.now():%Y%m%d-%H%M%S}.png"

    if shutil.which("gnome-screenshot"):
        try:
            result = _run(["gnome-screenshot", "-f", str(path)])
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and path.exists():
            return {"ok": True, "path": str(path), "method": "gnome-screenshot"}

    if shutil.which("gdbus"):
        try:
            result = _run(
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
                ]
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and path.exists():
            return {"ok": True, "path": str(path), "method": "gnome-shell"}
    return {"ok": False, "error": "I couldn't take a screenshot."}
