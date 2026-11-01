"""Ears: local speech-to-text with faster-whisper (Phase 3).

Runs `base.en` on the CPU with int8 weights, beam size 1 and VAD on. The model is
downloaded once into the project (`models/faster-whisper-base.en`) by
`main.py download-models`; afterwards it is loaded from disk with
`local_files_only=True`, so no internet is needed at runtime.

Audio is decoded with the stdlib `wave` module into a float32 numpy array and passed
to faster-whisper directly: faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`
for file paths, which `av` 19.x no longer accepts. Arrays skip that code path entirely.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from assistant.config import AssistantConfig
from assistant.safety.redact import redact_secrets

logger = logging.getLogger("ultron.ears")

SAMPLE_RATE = 16000
BEAM_SIZE = 1
NO_SPEECH = "I didn't hear anything."
UNCLEAR_SPEECH = "I didn't catch that, please say it again."
_BLANK_TRANSCRIPTS = {"", "[blank_audio]", "(silence)", "[silence]", "[no speech]", "[no_speech]"}

# "Say it again" guard. With clean audio the real scores are far from these limits:
# six good clips -0.14..-0.65 / 0.01..0.07 / 0.38..0.81, eight live turns
# -0.089..-0.343 / <=0.10 / <=1.233. Any ONE bad signal rejects.
@dataclass(frozen=True)
class GuardLimits:
    min_avg_logprob: float = -1.0
    max_no_speech_prob: float = 0.60
    max_compression_ratio: float = 2.4


DEFAULT_LIMITS = GuardLimits()
_WORD = re.compile(r"[a-z']+")


def has_real_word(text: str) -> bool:
    """True when the transcript contains at least one alphabetic word (not just noise)."""
    return any(len(word.strip("'")) >= 2 for word in _WORD.findall((text or "").lower()))


def looks_like_repetition_loop(text: str) -> bool:
    """True for decoder loops such as "mute mute unmute" or "list list".

    A normal command never has one word making up half of a short transcript.
    """
    words = _WORD.findall((text or "").lower())
    if len(words) < 2:
        return False
    top = Counter(words).most_common(1)[0][1]
    return top >= 2 and top / len(words) >= 0.5


def transcript_scores(segments: list[Any]) -> dict[str, float | None]:
    """Aggregate faster-whisper confidence: worst average, highest no-speech/compression."""
    averages = [getattr(s, "avg_logprob", None) for s in segments]
    no_speech = [getattr(s, "no_speech_prob", None) for s in segments]
    compression = [getattr(s, "compression_ratio", None) for s in segments]
    averages = [v for v in averages if v is not None]
    no_speech = [v for v in no_speech if v is not None]
    compression = [v for v in compression if v is not None]
    return {
        "min_avg_logprob": min(averages) if averages else None,
        "max_no_speech_prob": max(no_speech) if no_speech else None,
        "max_compression_ratio": max(compression) if compression else None,
    }


def _fmt_score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def guard_reason(text: str, segments: list[Any], limits: GuardLimits | None = None) -> str:
    """Return "" for reliable speech, otherwise a short reason for the rejection."""
    limits = limits or DEFAULT_LIMITS
    if not has_real_word(text):
        return "no_real_word"
    if looks_like_repetition_loop(text):
        return "repetition_loop"

    scores = transcript_scores(segments)
    avg = scores["min_avg_logprob"]
    no_speech = scores["max_no_speech_prob"]
    compression = scores["max_compression_ratio"]
    if avg is not None and avg < limits.min_avg_logprob:
        return f"avg_logprob={avg:.2f}"
    if no_speech is not None and no_speech > limits.max_no_speech_prob:
        return f"no_speech={no_speech:.2f}"
    if compression is not None and compression > limits.max_compression_ratio:
        return f"compression={compression:.2f}"
    return ""


def transcript_is_unreliable(text: str, segments: list[Any], limits: GuardLimits | None = None) -> bool:
    """Use faster-whisper's own confidence plus plain text rules to reject bad decodes."""
    return bool(guard_reason(text, segments, limits))


class EarsError(RuntimeError):
    """Raised when the local STT model is missing or cannot be loaded."""


def _resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear resample of mono float32 audio (only used if the WAV is not 16 kHz)."""
    if src_rate == dst_rate or samples.size == 0:
        return samples
    n_out = int(round(samples.size * dst_rate / src_rate))
    if n_out <= 0:
        return samples
    x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def load_wav_16k_mono(path: Path) -> np.ndarray:
    """Read a 16-bit PCM WAV into a mono float32 array at 16 kHz."""
    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
    except (OSError, EOFError, wave.Error) as exc:
        raise EarsError(f"Recorded WAV could not be read: {exc}") from exc

    if sampwidth != 2:
        raise EarsError(f"Unsupported WAV sample width {sampwidth}: expected 16-bit PCM.")
    if channels < 1 or nframes == 0 or not raw:
        return np.zeros(0, dtype=np.float32)

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(_resample(samples, rate, SAMPLE_RATE))


def _default_model_factory(model_dir: Path) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(str(model_dir), device="cpu", compute_type="int8", local_files_only=True)


def download_whisper_model(config: AssistantConfig, progress: Callable[[str], None] | None = None) -> Path:
    """One-time download of the faster-whisper model into the project's models/ folder."""
    note = progress or (lambda _msg: None)
    target = Path(config.whisper_model_dir)
    target.mkdir(parents=True, exist_ok=True)
    note(f"downloading faster-whisper '{config.whisper_model}' into {target} (one time, ~150 MB)")
    try:
        from faster_whisper.utils import download_model
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise EarsError("faster-whisper is not installed. Run: .venv/bin/python -m pip install faster-whisper") from exc

    saved = download_model(config.whisper_model, output_dir=str(target))
    note(f"faster-whisper model saved to {saved}")
    return Path(saved)


class WhisperEars:
    """faster-whisper wrapper that loads the model exactly once and stays offline afterwards."""

    def __init__(self, config: AssistantConfig, model_factory: Callable[[Path], Any] | None = None) -> None:
        self.config = config
        self._model: Any = None
        self._model_factory = model_factory or _default_model_factory
        self._load_lock = threading.Lock()
        # Filled by every transcribe() call: text, the three scores and the guard verdict.
        self.last_confidence: dict[str, Any] = {}

    def _guard_limits(self) -> GuardLimits:
        return GuardLimits(
            min_avg_logprob=float(self.config.whisper_min_avg_logprob),
            max_no_speech_prob=float(self.config.whisper_max_no_speech_prob),
            max_compression_ratio=float(self.config.whisper_max_compression_ratio),
        )

    @property
    def model_dir(self) -> Path:
        return Path(self.config.whisper_model_dir).expanduser()

    def is_downloaded(self) -> bool:
        """Return True when the model files are present in the project folder."""
        return (self.model_dir / "model.bin").is_file()

    def load(self) -> Any:
        """Load the model once (thread-safe, idempotent). Raise EarsError with a fix-it hint."""
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            if not self.is_downloaded():
                raise EarsError(
                    f"faster-whisper model not found in {self.model_dir}. "
                    f"Download it once with: .venv/bin/python main.py download-models"
                )
            started = time.monotonic()
            try:
                self._model = self._model_factory(self.model_dir)
            except Exception as exc:
                raise EarsError(f"Could not load faster-whisper from {self.model_dir}: {type(exc).__name__}: {exc}") from exc
            logger.info(
                "faster-whisper %s loaded from %s in %.2fs (cpu/int8)",
                self.config.whisper_model,
                self.model_dir,
                time.monotonic() - started,
            )
            return self._model

    def transcribe(self, wav_path: Path | str) -> tuple[bool, str]:
        """Transcribe one WAV file. Return (ok, text) where text is NO_SPEECH when nothing was said."""
        path = Path(wav_path)
        if not path.exists():
            return False, "Audio file missing."
        try:
            self.load()
            audio = load_wav_16k_mono(path)
        except EarsError as exc:
            logger.error("Ears unavailable: %s", redact_secrets(str(exc)))
            return False, str(exc)
        return self._transcribe_array(audio)

    def transcribe_samples(
        self,
        samples: np.ndarray,
        *,
        log_text: bool = True,
        hotwords: str | None = None,
        vad_filter: bool | None = None,
    ) -> tuple[bool, str]:
        """Transcribe a float32 mono array at 16 kHz (used by hands-free mode).

        No file is written, so audio that did not contain the wake word is never saved.
        ``log_text=False`` keeps the decoded words out of every log line; the words are
        still returned to the caller in memory.
        """
        try:
            self.load()
        except EarsError as exc:
            logger.error("Ears unavailable: %s", redact_secrets(str(exc)))
            return False, str(exc)
        audio = np.ascontiguousarray(samples.astype(np.float32, copy=False))
        return self._transcribe_array(audio, log_text=log_text, hotwords=hotwords, vad_filter=vad_filter)

    def _transcribe_array(
        self,
        audio: np.ndarray,
        *,
        log_text: bool = True,
        hotwords: str | None = None,
        vad_filter: bool | None = None,
    ) -> tuple[bool, str]:
        """Run faster-whisper on a float32 array and apply the confidence guard."""
        if audio.size == 0:
            logger.info("Ears: empty recording.")
            return False, NO_SPEECH

        started = time.monotonic()
        decode_options: dict[str, Any] = {
            "language": "en",
            "beam_size": BEAM_SIZE,
            "vad_filter": True,
            "condition_on_previous_text": False,
            "without_timestamps": True,
        }
        # No hotwords/initial_prompt by default: the decoder echoed the comma-separated
        # word list back into the transcript on unclear audio. Opt in explicitly only.
        if vad_filter is not None:
            decode_options["vad_filter"] = bool(vad_filter)
        if hotwords is not None:
            if hotwords:
                decode_options["hotwords"] = hotwords
        elif self.config.whisper_hotwords:
            decode_options["hotwords"] = self.config.whisper_hotwords
        try:
            model = self.load()
            segments, info = model.transcribe(audio, **decode_options)
            rows = list(segments)
            text = " ".join(segment.text.strip() for segment in rows).strip()
        except Exception as exc:
            err = f"Local transcription failed: {type(exc).__name__}: {exc}"
            logger.error(redact_secrets(err))
            return False, redact_secrets(err)

        elapsed = time.monotonic() - started
        audio_sec = audio.size / SAMPLE_RATE
        logger.info("Ears: %.2fs for %.1fs audio (lang=%s)", elapsed, audio_sec, getattr(info, "language", "en"))

        scores = transcript_scores(rows)
        reason = guard_reason(text, rows, self._guard_limits())
        verdict = reason or "ok"
        self.last_confidence = {**scores, "text": text, "verdict": verdict}
        if log_text:
            logger.info(
                "Ears confidence: min_avg_logprob=%s max_no_speech=%s max_compression=%s verdict=%s text=%r",
                _fmt_score(scores["min_avg_logprob"]),
                _fmt_score(scores["max_no_speech_prob"]),
                _fmt_score(scores["max_compression_ratio"]),
                verdict,
                redact_secrets(text),
            )
        else:
            # Hands-free mode: the words are never written to any log.
            logger.info(
                "Ears confidence: min_avg_logprob=%s max_no_speech=%s max_compression=%s verdict=%s text_chars=%d",
                _fmt_score(scores["min_avg_logprob"]),
                _fmt_score(scores["max_no_speech_prob"]),
                _fmt_score(scores["max_compression_ratio"]),
                verdict,
                len(text),
            )

        if text.strip().lower() in _BLANK_TRANSCRIPTS:
            logger.info("Ears: VAD/model reported no speech.")
            return False, NO_SPEECH
        if reason == "no_real_word":
            # A capped recording (a fan, room noise) decoded as punctuation only: the user
            # did not say anything, so this is "nothing heard", not "say it again".
            logger.info("Ears: no real word in the transcript; treating it as silence.")
            return False, NO_SPEECH
        if reason:
            logger.info("Ears: transcript rejected by the say-it-again guard (%s).", reason)
            return False, UNCLEAR_SPEECH
        return True, text.strip()
