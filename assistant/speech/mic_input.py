"""Optional microphone input level, applied once at startup (no sudo).

The live tests run with the PipeWire source at about 0.21. Setting it once from .env keeps
the level stable across sessions without touching system settings. Failures are logged as
one line and never block startup.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from typing import Any, Callable

DEFAULT_SOURCE = "@DEFAULT_AUDIO_SOURCE@"
MIN_VOLUME = 0.05
MAX_VOLUME = 0.6
# Brave/Chromium WebRTC AGC can drive the PipeWire source to full scale during a session.
# A raw peak at/above 32700 with RMS above 3500 is the live signature measured on Nov 3.
AGC_PEAK_THRESHOLD = 32700
AGC_RMS_THRESHOLD = 3500
AGC_RESTORE_VOLUME = "0.21"
AGC_RESTORE_DELAY_S = 1.5

logger = logging.getLogger("ultron.mic")


def apply_mic_source_volume(
    value: object,
    *,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[bool, str]:
    """Set the default mic source volume once when ``MIC_SOURCE_VOLUME`` is configured."""
    run = runner or subprocess.run
    find = which or shutil.which

    raw = str(value or "").strip()
    if not raw:
        return False, "MIC_SOURCE_VOLUME not set"
    try:
        volume = float(raw)
    except (TypeError, ValueError):
        logger.info("MIC_SOURCE_VOLUME ignored: %r is not a number.", raw)
        return False, "MIC_SOURCE_VOLUME is not a number"
    if not (MIN_VOLUME <= volume <= MAX_VOLUME):
        logger.info(
            "MIC_SOURCE_VOLUME ignored: %.2f is outside %.2f-%.2f.", volume, MIN_VOLUME, MAX_VOLUME
        )
        return False, f"MIC_SOURCE_VOLUME {volume:.2f} is outside {MIN_VOLUME:.2f}-{MAX_VOLUME:.2f}"

    if find("wpctl") is None:
        logger.info("MIC_SOURCE_VOLUME ignored: wpctl not found.")
        return False, "wpctl not found"

    target = f"{volume:.2f}"
    cmd = ["wpctl", "set-volume", DEFAULT_SOURCE, target]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except Exception as exc:  # wpctl is optional; never block chat/voice-test
        logger.info("MIC_SOURCE_VOLUME ignored: wpctl failed (%s: %s).", type(exc).__name__, exc)
        return False, f"wpctl failed: {type(exc).__name__}"

    if getattr(proc, "returncode", 1) != 0:
        detail = ((getattr(proc, "stderr", "") or getattr(proc, "stdout", "")) or "").strip()[:200]
        logger.info("MIC_SOURCE_VOLUME ignored: wpctl set-volume failed (%s).", detail or "non-zero exit")
        return False, "wpctl set-volume failed"

    logger.info("Mic source volume set to %s (%s).", target, DEFAULT_SOURCE)
    return True, f"Mic source volume set to {target}"


def ensure_mic_unmuted(
    *,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    force: bool = False,
) -> bool:
    """Un-mute the default source at startup so the microphone is never left muted.

    A previous session that ended mid-utterance (or a TTS guard crash) can leave
    ``@DEFAULT_AUDIO_SOURCE@`` muted forever, which silently kills every listener.
    """
    if not force and runner is None and _tests_must_not_touch_hardware():
        return False
    run = runner or subprocess.run
    find = which or shutil.which
    if find("wpctl") is None:
        return False
    try:
        proc = run(
            ["wpctl", "set-mute", DEFAULT_SOURCE, "0"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # wpctl is optional; never block chat/voice startup
        logger.info("Mic unmute ignored: wpctl failed (%s: %s).", type(exc).__name__, exc)
        return False
    if getattr(proc, "returncode", 1) != 0:
        logger.info("Mic unmute ignored: wpctl set-mute failed.")
        return False
    return True


def configured_restore_volume(value: object = None, *, env: dict[str, str] | None = None) -> str:
    """Return MIC_SOURCE_VOLUME when valid, else the measured-safe 0.21."""
    source = env if env is not None else os.environ
    raw = str(value if value is not None else source.get("MIC_SOURCE_VOLUME", "")).strip()
    try:
        volume = float(raw)
    except (TypeError, ValueError):
        return AGC_RESTORE_VOLUME
    if not (MIN_VOLUME <= volume <= MAX_VOLUME):
        return AGC_RESTORE_VOLUME
    return f"{volume:.2f}"


def agc_guard_needed(peak: int, rms: int) -> bool:
    """True only for the Brave/Chromium AGC signature (clipped peak plus a loud body)."""
    return int(peak or 0) >= AGC_PEAK_THRESHOLD and int(rms or 0) > AGC_RMS_THRESHOLD


def _tests_must_not_touch_hardware() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ULTRON_NO_MIC"))


def restore_mic_volume(
    value: object = None,
    *,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[bool, str]:
    """Set the source back to MIC_SOURCE_VOLUME (or 0.21). Safe to call from a thread."""
    target = configured_restore_volume(value)
    logger.info("Mic AGC guard: restoring %s to %s.", DEFAULT_SOURCE, target)
    return apply_mic_source_volume(target, runner=runner, which=which)


def guard_mic_agc(
    peak: int,
    rms: int,
    value: object = None,
    *,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    background: bool = True,
    force: bool = False,
) -> bool:
    """Undo an external AGC boost after a hot utterance.

    Production runs the ``wpctl`` call in a daemon thread so the next utterance is not
    delayed. Tests pass ``runner``/``force`` and ``background=False``; without that, the
    ``PYTEST_CURRENT_TEST`` guard keeps every test away from the real microphone.
    """
    if not agc_guard_needed(peak, rms):
        return False
    if not force and runner is None and _tests_must_not_touch_hardware():
        logger.info("Mic AGC guard skipped under pytest/ULTRON_NO_MIC.")
        return False
    if background:
        thread = threading.Thread(
            target=restore_mic_volume,
            kwargs={"value": value, "runner": runner, "which": which},
            name="mic-agc-restore",
            daemon=True,
        )
        thread.start()
        return True
    ok, _detail = restore_mic_volume(value, runner=runner, which=which)
    return ok


def schedule_mic_volume_restore(
    delay_s: float = AGC_RESTORE_DELAY_S,
    value: object = None,
    *,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    force: bool = False,
) -> threading.Timer | None:
    """Re-apply the safe source level 1.5 s after a browser launches."""
    if not force and runner is None and _tests_must_not_touch_hardware():
        return None
    timer = threading.Timer(
        max(0.0, float(delay_s)),
        restore_mic_volume,
        kwargs={"value": value, "runner": runner, "which": which},
    )
    timer.daemon = True
    timer.start()
    return timer
