"""Three independent kill switches: GNOME gsettings Ctrl+Alt+Q hotkey, software watchdog, and stop signal."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

from assistant.config import LOGS_DIR, PROJECT_ROOT

KILL_FLAG_FILE = LOGS_DIR / ".kill_switch"
WORKER_PIDS_FILE = LOGS_DIR / ".worker_pids.json"


def _read_worker_pids() -> list[int]:
    if not WORKER_PIDS_FILE.exists():
        return []
    try:
        return [int(x) for x in json.loads(WORKER_PIDS_FILE.read_text(encoding="utf-8"))]
    except Exception:
        return []


def trigger_kill_switch(reason: str = "Manual kill switch triggered") -> dict[str, object]:
    """Write kill flag and terminate any registered worker PIDs."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    KILL_FLAG_FILE.write_text(reason, encoding="utf-8")
    killed: list[int] = []
    for pid in _read_worker_pids():
        if pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except Exception:
                pass
    WORKER_PIDS_FILE.write_text("[]", encoding="utf-8")
    return {"triggered": True, "reason": reason, "killed_pids": killed}


def clear_kill_switch() -> None:
    if KILL_FLAG_FILE.exists():
        KILL_FLAG_FILE.unlink(missing_ok=True)


def is_kill_switch_tripped() -> tuple[bool, str]:
    if KILL_FLAG_FILE.exists():
        try:
            return True, KILL_FLAG_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            return True, "Kill switch active"
    return False, ""


def register_gnome_kill_hotkey(binding: str = "<Ctrl><Alt>q") -> tuple[bool, str]:
    """Bind Ctrl+Alt+Q on GNOME 50 Wayland via gsettings to invoke `python3 main.py stop`."""
    stop_cmd = f"{sys.executable} {PROJECT_ROOT / 'main.py'} stop"
    schema_base = "org.gnome.settings-daemon.plugins.media-keys"
    custom_path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/ultron-kill/"
    item_schema = f"{schema_base}.custom-keybinding:{custom_path}"
    try:
        res = subprocess.run(
            ["gsettings", "get", schema_base, "custom-keybindings"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode != 0:
            return True, f"gsettings inactive; software watchdog ready ({stop_cmd})"
        raw = res.stdout.strip()
        paths = [p.strip().strip("'\"") for p in raw.strip("[]").split(",") if p.strip() and not raw.startswith("@as")]
        if custom_path not in paths:
            paths.append(custom_path)
        formatted = "[" + ", ".join(f"'{p}'" for p in paths) + "]"
        subprocess.run(["gsettings", "set", schema_base, "custom-keybindings", formatted], timeout=3, check=False)
        subprocess.run(["gsettings", "set", item_schema, "name", "Ultron Emergency Stop"], timeout=3, check=False)
        subprocess.run(["gsettings", "set", item_schema, "command", stop_cmd], timeout=3, check=False)
        subprocess.run(["gsettings", "set", item_schema, "binding", binding], timeout=3, check=False)
        return True, f"Registered GNOME kill hotkey {binding} -> {stop_cmd}"
    except FileNotFoundError:
        return True, "gsettings not installed; software watchdog active."
    except Exception as exc:
        return False, f"Failed to register kill hotkey: {exc}"


class WatchdogMonitor:
    """Software watchdog monitoring active tasks for step cap, wall-clock cap, and kill signal."""

    def __init__(self, max_steps: int = 25, wall_clock_sec: int = 180, idle_sec: int = 30) -> None:
        self.max_steps = max_steps
        self.wall_clock_sec = wall_clock_sec
        self.idle_sec = idle_sec
        self.current_steps = 0
        self.task_start_time: float | None = None
        self.abort_reason: str = ""
        self._stop_event = threading.Event()

    def start_task(self) -> None:
        clear_kill_switch()
        self.current_steps = 0
        self.abort_reason = ""
        self.task_start_time = time.monotonic()
        self._stop_event.clear()
        threading.Thread(target=self._watch_loop, daemon=True).start()

    def record_step(self) -> int:
        self.current_steps += 1
        if self.current_steps > self.max_steps:
            self.abort_reason = f"Watchdog killed task: exceeded step cap ({self.max_steps})."
            trigger_kill_switch(self.abort_reason)
        return self.current_steps

    def stop_task(self) -> None:
        self._stop_event.set()
        self.task_start_time = None

    def check_ok(self) -> tuple[bool, str]:
        tripped, reason = is_kill_switch_tripped()
        if tripped:
            self.abort_reason = reason
            return False, reason
        if self.abort_reason:
            return False, self.abort_reason
        if self.task_start_time is not None and (time.monotonic() - self.task_start_time) > self.wall_clock_sec:
            self.abort_reason = f"Watchdog killed task: exceeded wall-clock limit ({self.wall_clock_sec}s)."
            trigger_kill_switch(self.abort_reason)
            return False, self.abort_reason
        return True, ""

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(0.25):
            ok, _ = self.check_ok()
            if not ok:
                break
