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
WHAT_VOLUME_REPLY = "What volume should I set? Say a number from 0 to 100."

_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def _number_words(tokens: list[str]) -> list[int]:
    """Turn a token run such as 'thirty' / 'twenty five' / 'one hundred' into numbers."""
    values: list[int] = []
    current = 0
    seen = False
    for token in tokens:
        if token in _ONES:
            current += _ONES[token]
            seen = True
        elif token in _TEENS:
            current += _TEENS[token]
            seen = True
        elif token in _TENS:
            current += _TENS[token]
            seen = True
        elif token == "hundred":
            current = (current or 1) * 100
            seen = True
        elif seen:
            values.append(current)
            current = 0
            seen = False
    if seen:
        values.append(current)
    return values


def numbers_in_text(text: str) -> set[int]:
    """Return every number the user said, as digits or English number words."""
    tokens = re.findall(r"[a-z]+|\d+", (text or "").lower().replace("-", " "))
    numbers = {int(token) for token in tokens if token.isdigit()}
    numbers.update(_number_words(tokens))
    return numbers


def percent_is_mentioned(percent: object, text: str) -> bool:
    """True only when the exact percent value appears in the user's own transcript."""
    try:
        wanted = int(round(float(percent)))
    except (TypeError, ValueError):
        return False
    return wanted in numbers_in_text(text)


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
