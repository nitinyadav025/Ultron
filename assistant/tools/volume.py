"""Safe volume control tools via wpctl (PipeWire/WirePlumber)."""

from __future__ import annotations

import re
import subprocess
from typing import Any

from assistant.tools.registry import tool

DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"
MIN_STEP_PERCENT = 5
MAX_STEP_PERCENT = 20
MIN_VOLUME_PERCENT = 0
MAX_VOLUME_PERCENT = 100
MAX_VOLUME = 1.0


def clamp_step(step_percent: int) -> int:
    """Clamp a requested volume step to the safe 5-20 percent range."""
    try:
        step = int(step_percent)
    except (TypeError, ValueError):
        step = 10
    return max(MIN_STEP_PERCENT, min(MAX_STEP_PERCENT, step))


def clamp_percent(percent: int) -> int:
    """Clamp an absolute volume request to 0-100 percent."""
    try:
        value = int(round(float(percent)))
    except (TypeError, ValueError):
        value = 50
    return max(MIN_VOLUME_PERCENT, min(MAX_VOLUME_PERCENT, value))


def _get_volume() -> float | None:
    try:
        proc = subprocess.run(
            ["wpctl", "get-volume", DEFAULT_SINK],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"Volume:\s*([0-9]*\.?[0-9]+)", proc.stdout or "")
    return float(match.group(1)) if match else None


def _set_volume(target: float) -> dict[str, Any]:
    target = max(0.0, min(MAX_VOLUME, target))
    try:
        proc = subprocess.run(
            ["wpctl", "set-volume", DEFAULT_SINK, f"{target:.3f}", "--limit", "1.0"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"wpctl unavailable: {exc}"}
    if proc.returncode != 0:
        detail = ((proc.stderr or "") or (proc.stdout or "")).strip()[:300]
        return {"ok": False, "error": detail or "wpctl set-volume failed."}
    return {"ok": True, "volume_percent": round(target * 100), "sink": DEFAULT_SINK}


@tool(risk_level="safe", read_only=False)
def volume_up(step_percent: int = 10) -> dict[str, Any]:
    """Raise output volume."""
    step = clamp_step(step_percent)
    current = _get_volume()
    if current is None:
        return {"ok": False, "error": "Could not read the current volume via wpctl."}
    result = _set_volume(current + step / 100.0)
    result["step_percent"] = step
    return result


@tool(risk_level="safe", read_only=False)
def volume_down(step_percent: int = 10) -> dict[str, Any]:
    """Lower output volume."""
    step = clamp_step(step_percent)
    current = _get_volume()
    if current is None:
        return {"ok": False, "error": "Could not read the current volume via wpctl."}
    result = _set_volume(current - step / 100.0)
    result["step_percent"] = step
    return result


@tool(risk_level="safe", read_only=False)
def set_volume(percent: int = 50) -> dict[str, Any]:
    """Set output volume to a percent (0-100)."""
    target = clamp_percent(percent)
    result = _set_volume(target / 100.0)
    result["requested_percent"] = target
    return result


@tool(risk_level="safe", read_only=False)
def mute_audio(muted: bool = True) -> dict[str, Any]:
    """Mute or unmute output; muted=false unmutes."""
    try:
        proc = subprocess.run(
            ["wpctl", "set-mute", DEFAULT_SINK, "1" if muted else "0"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"wpctl unavailable: {exc}"}
    if proc.returncode != 0:
        detail = ((proc.stderr or "") or (proc.stdout or "")).strip()[:300]
        return {"ok": False, "error": detail or "wpctl set-mute failed."}
    return {"ok": True, "muted": bool(muted), "sink": DEFAULT_SINK}
