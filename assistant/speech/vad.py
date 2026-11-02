"""Silero VAD wrapper bundled with faster-whisper (no download, no new package).

The energy detector alone cannot tell a loud, steady fan (RMS ~1700) from a voice. Silero
does: it returns 0 ms of human speech for the fan and real speech for the user. The wrapper
is optional and failure-tolerant: if onnxruntime or the bundled model is unavailable,
``speech_ms_in_window`` returns ``None`` and the caller falls back to the energy rules.

Nothing here loads a file or opens a microphone. Tests inject a fake object with the same
``speech_ms_in_window`` method, so the suite never pays for the ONNX model.
"""

from __future__ import annotations

import array
import logging
import threading
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger("ultron.vad")

SAMPLE_RATE = 16000
DEFAULT_WINDOW_MS = 850
DEFAULT_CONTEXT_MS = 900


def _as_float32(audio: Any) -> np.ndarray:
    """Accept a float32 array, int16 array, or a list of int16 ``array.array`` frames."""
    if isinstance(audio, np.ndarray):
        if audio.dtype == np.int16:
            return audio.astype(np.float32) / 32768.0
        return np.ascontiguousarray(audio.astype(np.float32, copy=False))
    if isinstance(audio, (bytes, bytearray)):
        raw = np.frombuffer(bytes(audio), dtype="<i2").astype(np.float32)
        return raw / 32768.0
    if isinstance(audio, array.array):
        if audio.typecode != "h":
            return np.asarray(audio, dtype=np.float32)
        return np.frombuffer(audio.tobytes(), dtype="<i2").astype(np.float32) / 32768.0
    if not audio:
        return np.zeros(0, dtype=np.float32)
    merged = array.array("h")
    for frame in audio:
        if isinstance(frame, array.array):
            merged.extend(frame)
        else:
            merged.frombytes(bytes(frame))
    return np.frombuffer(merged.tobytes(), dtype="<i2").astype(np.float32) / 32768.0


class SileroVad:
    """Small thread-safe wrapper around ``faster_whisper.vad.get_speech_timestamps``."""

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        min_speech_duration_ms: int = 100,
        min_silence_duration_ms: int = 100,
        speech_pad_ms: int = 0,
    ) -> None:
        self.threshold = float(threshold)
        self.min_speech_duration_ms = int(min_speech_duration_ms)
        self.min_silence_duration_ms = int(min_silence_duration_ms)
        self.speech_pad_ms = int(speech_pad_ms)
        self._lock = threading.Lock()
        self._failed = False
        self.last_error = ""

    def _options(self) -> Any:
        from faster_whisper.vad import VadOptions

        return VadOptions(
            threshold=self.threshold,
            min_speech_duration_ms=self.min_speech_duration_ms,
            min_silence_duration_ms=self.min_silence_duration_ms,
            speech_pad_ms=self.speech_pad_ms,
        )

    def speech_ms(self, audio: Any) -> int | None:
        """Total human speech in ``audio`` in milliseconds, or None when VAD is unusable."""
        samples = _as_float32(audio)
        if samples.size == 0:
            return 0
        if self._failed:
            return None
        try:
            from faster_whisper.vad import get_speech_timestamps

            with self._lock:
                segments = get_speech_timestamps(samples, self._options())
        except Exception as exc:  # onnxruntime/model missing or broken
            self._failed = True
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Silero VAD unavailable; using energy endpointing only (%s)", self.last_error)
            return None
        total = 0.0
        for segment in segments:
            total += max(0.0, float(segment["end"]) - float(segment["start"]))
        return int(round(total * 1000.0 / SAMPLE_RATE))

    def speech_ms_in_window(
        self,
        audio: Any,
        *,
        window_ms: int = DEFAULT_WINDOW_MS,
        context_ms: int = DEFAULT_CONTEXT_MS,
    ) -> int | None:
        """Speech inside the last ``window_ms`` of a ``context_ms`` tail.

        The extra 50 ms of context lets Silero settle its first window. A segment that began
        before the evaluated window is clipped, so a word that ended 100 ms ago is not
        counted as "still speaking".
        """
        samples = _as_float32(audio)
        if samples.size == 0:
            return 0
        context_samples = max(1, int(context_ms * SAMPLE_RATE / 1000))
        if samples.size > context_samples:
            samples = samples[-context_samples:]
        total_ms = samples.size * 1000.0 / SAMPLE_RATE
        offset_ms = max(0.0, total_ms - float(window_ms))
        if self._failed:
            return None
        try:
            from faster_whisper.vad import get_speech_timestamps

            with self._lock:
                segments = get_speech_timestamps(samples, self._options())
        except Exception as exc:
            self._failed = True
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Silero VAD unavailable; using energy endpointing only (%s)", self.last_error)
            return None
        spoken = 0.0
        for segment in segments:
            start = float(segment["start"]) * 1000.0 / SAMPLE_RATE
            end = float(segment["end"]) * 1000.0 / SAMPLE_RATE
            spoken += max(0.0, min(end, total_ms) - max(start, offset_ms))
        return int(round(min(spoken, float(window_ms))))


_SHARED: SileroVad | None = None
_SHARED_LOCK = threading.Lock()


def get_shared_vad() -> SileroVad:
    """One model wrapper per process (the ONNX model itself is cached by faster-whisper)."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = SileroVad()
        return _SHARED
