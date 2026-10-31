"""Sandboxed Scratch Runner using systemd-run --user --scope (with timeout fallback)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from assistant.config import WORKSPACE_DIR


def build_sandbox_command(
    script_path: Path,
    timeout_sec: int = 30,
    memory_max: str = "512M",
    cpu_quota: str = "25%",
) -> list[str]:
    """Build systemd-run user scope command or fallback to timeout."""
    py_cmd = [sys.executable, "-I", str(script_path)]
    if shutil.which("systemd-run"):
        chk = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=2, check=False)
        if chk.returncode == 0 or "running" in chk.stdout or "degraded" in chk.stdout:
            return [
                "systemd-run", "--user", "--scope", "--quiet",
                "-p", f"MemoryMax={memory_max}",
                "-p", f"CPUQuota={cpu_quota}",
                "-p", f"RuntimeMaxSec={timeout_sec}",
                *py_cmd,
            ]
    return ["timeout", "--kill-after=2", str(timeout_sec), *py_cmd]


def run_sandboxed_script(
    code: str,
    filename: str = "scratch_task.py",
    timeout_sec: int = 30,
    memory_max: str = "512M",
    cpu_quota: str = "25%",
) -> dict[str, Any]:
    """Write a Python script inside workspace/ and execute it with CPU, RAM, and timeout caps."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    if not safe_name.endswith(".py"):
        safe_name += ".py"
    script_path = (WORKSPACE_DIR / safe_name).resolve()
    script_path.write_text(code, encoding="utf-8")
    cmd = build_sandbox_command(script_path, timeout_sec, memory_max, cpu_quota)

    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(WORKSPACE_DIR), capture_output=True, text=True, timeout=timeout_sec + 2, check=False)
        elapsed = round(time.perf_counter() - start, 2)
        killed = proc.returncode in (124, 137, 143, -9, -15) or elapsed >= timeout_sec
        return {
            "ok": proc.returncode == 0 and not killed,
            "killed_by_timeout": killed,
            "returncode": proc.returncode,
            "duration_sec": elapsed,
            "stdout": proc.stdout[:4000],
            "stderr": proc.stderr[:4000],
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "killed_by_timeout": True,
            "returncode": -9,
            "duration_sec": round(time.perf_counter() - start, 2),
            "stdout": "",
            "stderr": f"Script killed after exceeding {timeout_sec}s timeout cap.",
        }
