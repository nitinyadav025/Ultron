"""Optional microphone input level, applied once at startup (no sudo).

The live tests run with the PipeWire source at about 0.21. Setting it once from .env keeps
the level stable across sessions without touching system settings. Failures are logged as
one line and never block startup.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any, Callable

DEFAULT_SOURCE = "@DEFAULT_AUDIO_SOURCE@"
MIN_VOLUME = 0.05
MAX_VOLUME = 0.6

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
