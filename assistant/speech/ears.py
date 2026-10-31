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
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from assistant.config import PROJECT_ROOT, AssistantConfig
from assistant.safety.redact import redact_secrets

logger = logging.getLogger("ultron.ears")

SAMPLE_RATE = 16000
BEAM_SIZE = 1
NO_SPEECH = "I didn't catch that"
_BLANK_TRANSCRIPTS = {"", "[blank_audio]", "(silence)", "[silence]", "[no speech]", "[no_speech]"}

# Whisper mishears this project's short command words ("open firefox" came back as
# "Open 5 box"). faster-whisper 1.2.1 supports `hotwords`, which biases the decoder
# without feeding fake previous text (that is what `initial_prompt` would do).
TOOL_WORDS = (
    "Ultron, open, list files, read file, volume, volume up, volume down, set volume to, "
    "mute, unmute, percent, Downloads, Documents, Desktop"
)


def load_app_names(apps_file: Path | None = None) -> list[str]:
    """Read the app names Ultron can launch from config/apps.yaml (never raises)."""
    path = apps_file or (PROJECT_ROOT / "config" / "apps.yaml")
    try:
        import yaml

        catalog = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("apps", {})
        return [str(name) for name in catalog]
    except Exception:  # missing/broken YAML must never break the microphone path
        return []


def build_hotwords(apps_file: Path | None = None) -> str:
    """Build the hotword hint from the real app names plus the tool trigger words."""
    words: list[str] = []
    seen: set[str] = set()
    for word in [*load_app_names(apps_file), *TOOL_WORDS.split(",")]:
        cleaned = word.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            words.append(cleaned)
    return ", ".join(words)


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
            model = self.load()
            audio = load_wav_16k_mono(path)
        except EarsError as exc:
            logger.error("Ears unavailable: %s", redact_secrets(str(exc)))
            return False, str(exc)

        if audio.size == 0:
            logger.info("Ears: empty recording.")
            return False, NO_SPEECH

        started = time.monotonic()
        try:
            segments, info = model.transcribe(
                audio,
                language="en",
                beam_size=BEAM_SIZE,
                vad_filter=True,
                condition_on_previous_text=False,
                without_timestamps=True,
                hotwords=self.config.whisper_hotwords or build_hotwords(),
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            err = f"Local transcription failed: {type(exc).__name__}: {exc}"
            logger.error(redact_secrets(err))
            return False, redact_secrets(err)

        elapsed = time.monotonic() - started
        audio_sec = audio.size / SAMPLE_RATE
        logger.info("Ears: %.2fs for %.1fs audio (lang=%s)", elapsed, audio_sec, getattr(info, "language", "en"))

        if text.strip().lower() in _BLANK_TRANSCRIPTS:
            logger.info("Ears: VAD/model reported no speech.")
            return False, NO_SPEECH
        return True, text.strip()
