"""Mouth: local text-to-speech with Piper (Phase 4).

Voice `en_US-lessac-medium` runs on the CPU at its native 22.05 kHz. The voice is
downloaded once into `models/piper/` by `main.py download-models` and loaded from
there at startup, so runtime needs no internet.

Audio handling for clarity:
* sentences are synthesized separately so the first one can play while the rest generate;
* a short lead-in and tail of silence (120 ms / 200 ms) is added so pw-play never
  clips the first or last phoneme;
* samples are scaled by VOICE_VOLUME with a 0.95 ceiling and hard int16 clipping
  protection, so nothing distorts;
* the WAV header uses the voice's real sample rate (never a guessed one).
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from assistant.config import WORKSPACE_DIR, AssistantConfig, clamp_voice_volume
from assistant.safety.redact import redact_secrets

logger = logging.getLogger("ultron.mouth")

LEAD_SILENCE_MS = 120
TAIL_SILENCE_MS = 200
PEAK_CEILING = 0.95  # leave a little headroom so nothing clips
MAX_SENTENCES = 6


class MouthError(RuntimeError):
    """Raised when the Piper voice is missing or cannot produce audio."""


def split_sentences(text: str, max_sentences: int = MAX_SENTENCES) -> list[str]:
    """Split a reply into sentences so each can be spoken as soon as it is ready."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", cleaned) if p.strip()]
    merged: list[str] = []
    for part in parts:
        # Glue tiny fragments ("Hi.") that would sound like separate utterances.
        if merged and len(part) < 12 and not part.endswith(("?", "!")):
            merged[-1] = f"{merged[-1]} {part}".strip()
        else:
            merged.append(part)
    return merged[:max_sentences]


def download_piper_voice(config: AssistantConfig, progress: Callable[[str], None] | None = None) -> Path:
    """One-time download of the Piper voice (.onnx + .onnx.json) into the project's models/ dir."""
    note = progress or (lambda _msg: None)
    target = Path(config.piper_voice_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    note(f"downloading Piper voice '{config.piper_voice}' into {target} (one time, ~63 MB)")
    try:
        from piper.download_voices import download_voice
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise MouthError("piper-tts is not installed. Run: .venv/bin/python -m pip install piper-tts") from exc

    download_voice(config.piper_voice, download_dir=target)
    note(f"Piper voice saved to {target}")
    return target


def _default_voice_factory(model_path: Path, config_path: Path) -> Any:
    from piper import PiperVoice

    return PiperVoice.load(str(model_path), str(config_path))


class PiperMouth:
    """Piper wrapper that loads the voice exactly once and synthesizes WAV bytes per sentence."""

    def __init__(self, config: AssistantConfig, voice_factory: Callable[[Path, Path], Any] | None = None) -> None:
        self.config = config
        self._voice: Any = None
        self._voice_factory = voice_factory or _default_voice_factory
        self._load_lock = threading.Lock()
        # Barge-in: the active pw-play/paplay/aplay process is tracked here so a Dashboard
        # Stop/Dismiss or a spoken interrupt can kill it immediately (<20 ms target).
        self._player_lock = threading.Lock()
        self._active_player: subprocess.Popen[bytes] | None = None
        self._stop_event = threading.Event()
        self._speaking = False

    # -- live state --------------------------------------------------------------

    @staticmethod
    def _state() -> Any:
        try:
            from assistant.environment.state import get_state

            return get_state()
        except Exception:
            return None

    def _publish(self, *, status: str | None = None, rms: int | None = None, peak: int | None = None) -> None:
        state = self._state()
        if state is None:
            return
        try:
            if status:
                state.set_status(status)
            if rms is not None and peak is not None:
                state.set_audio_levels(rms, peak)
        except Exception:
            pass

    @property
    def is_speaking(self) -> bool:
        with self._player_lock:
            proc = self._active_player
        return self._speaking or (proc is not None and proc.poll() is None)

    def adopt_player(self, proc: subprocess.Popen[Any] | None) -> None:
        """Let the CLI/GUI voice path share its pw-play handle with the barge-in switch."""
        with self._player_lock:
            self._active_player = proc  # type: ignore[assignment]

    def release_player(self, proc: subprocess.Popen[Any] | None) -> None:
        with self._player_lock:
            if self._active_player is proc:
                self._active_player = None

    def stop_speaking(self) -> bool:
        """Terminate the active playback process now; safe to call from any thread."""
        self._stop_event.set()
        with self._player_lock:
            proc = self._active_player
            self._active_player = None
        killed = False
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=0.02)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=0.2)
                except Exception:
                    pass
            killed = True
        self._speaking = False
        self._publish(status="online", rms=0, peak=0)
        return killed

    @staticmethod
    def _player_argv(path: Path) -> list[str]:
        if shutil.which("pw-play"):
            return ["pw-play", str(path)]
        if shutil.which("paplay"):
            return ["paplay", str(path)]
        if shutil.which("aplay"):
            return ["aplay", "-q", str(path)]
        return []

    @staticmethod
    def _wav_envelope(wav_bytes: bytes) -> tuple[int, int]:
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype="<i2")
            if samples.size == 0:
                return 0, 0
            values = samples.astype(np.float64)
            return int(np.sqrt(np.mean(values * values))), int(np.max(np.abs(samples)))
        except Exception:
            return 0, 0

    def play_wav_bytes(self, wav_bytes: bytes, *, filename: str = "voice_output_env.wav") -> tuple[bool, str]:
        """Play one WAV and track its process for barge-in."""
        target = Path(WORKSPACE_DIR) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(wav_bytes)
        argv = self._player_argv(target)
        if not argv:
            return False, "No audio player found (install pipewire-utils for pw-play)."
        self._stop_event.clear()
        rms, peak = self._wav_envelope(wav_bytes)
        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            with self._player_lock:
                self._active_player = proc
                self._speaking = True
            self._publish(status="speaking", rms=rms, peak=peak)
            try:
                proc.wait(timeout=60.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
            ok = proc.returncode in (0, -15, -9) and not self._stop_event.is_set()
            return ok, argv[0] if ok else "Playback was interrupted or failed."
        except OSError as exc:
            return False, f"Audio playback failed: {type(exc).__name__}: {exc}"
        finally:
            with self._player_lock:
                if self._active_player is proc:
                    self._active_player = None
                self._speaking = False
            self._publish(status="online", rms=0, peak=0)

    def speak(self, text: str) -> tuple[bool, str]:
        """Synthesize and play a reply sentence by sentence; barge-in stops every sentence."""
        sentences = split_sentences(text)
        if not sentences:
            return True, "Nothing to speak."
        self._stop_event.clear()
        spoken = 0
        player = ""
        for sentence in sentences:
            if self._stop_event.is_set():
                break
            try:
                wav_bytes = self.synthesize_wav(sentence)
            except MouthError as exc:
                return spoken > 0, str(exc)
            ok, info = self.play_wav_bytes(wav_bytes)
            if not ok:
                return spoken > 0, info
            player = info
            spoken += 1
        return True, f"Piper ({self.config.piper_voice}) via {player}"

    @property
    def voice_dir(self) -> Path:
        return Path(self.config.piper_voice_dir).expanduser()

    @property
    def model_path(self) -> Path:
        return self.voice_dir / f"{self.config.piper_voice}.onnx"

    @property
    def config_path(self) -> Path:
        return self.voice_dir / f"{self.config.piper_voice}.onnx.json"

    def is_downloaded(self) -> bool:
        return self.model_path.is_file() and self.config_path.is_file()

    def load(self) -> Any:
        """Load the voice once (thread-safe). Raise MouthError with a fix-it hint."""
        if self._voice is not None:
            return self._voice
        with self._load_lock:
            if self._voice is not None:
                return self._voice
            if not self.is_downloaded():
                raise MouthError(
                    f"Piper voice not found in {self.voice_dir}. "
                    f"Download it once with: .venv/bin/python main.py download-models"
                )
            started = time.monotonic()
            try:
                self._voice = self._voice_factory(self.model_path, self.config_path)
            except Exception as exc:
                raise MouthError(
                    f"Could not load Piper voice {self.model_path.name}: {type(exc).__name__}: {exc}"
                ) from exc
            logger.info(
                "Piper %s loaded from %s in %.2fs (cpu)",
                self.config.piper_voice,
                self.voice_dir,
                time.monotonic() - started,
            )
            return self._voice

    def sample_rate(self) -> int:
        """Native sample rate of the loaded voice (22050 Hz for lessac-medium)."""
        voice = self.load()
        return int(getattr(getattr(voice, "config", None), "sample_rate", 22050))

    def synthesize_pcm(self, text: str) -> tuple[bytes, int]:
        """Synthesize one sentence and return (int16 PCM with lead/tail silence, sample rate)."""
        voice = self.load()
        try:
            from piper.config import SynthesisConfig

            chunks = list(voice.synthesize(text, syn_config=SynthesisConfig()))
        except Exception as exc:
            err = redact_secrets(f"Piper synthesis failed: {type(exc).__name__}: {exc}")
            logger.error(err)
            raise MouthError(err) from exc

        if not chunks:
            raise MouthError("Piper produced no audio for this sentence.")

        rate = int(getattr(chunks[0], "sample_rate", 22050))
        width = int(getattr(chunks[0], "sample_width", 2))
        channels = int(getattr(chunks[0], "sample_channels", 1))
        if width != 2 or channels != 1:
            raise MouthError(f"Unexpected Piper output format (width={width}, channels={channels}).")

        pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        gain = min(1.0, clamp_voice_volume(self.config.voice_volume)) * PEAK_CEILING
        samples = np.clip(samples * gain, -32768.0, 32767.0).astype("<i2")

        lead = np.zeros(int(rate * LEAD_SILENCE_MS / 1000), dtype="<i2")
        tail = np.zeros(int(rate * TAIL_SILENCE_MS / 1000), dtype="<i2")
        padded = np.concatenate([lead, samples, tail]).astype("<i2")
        logger.info("Piper spoke %d chars -> %.2fs audio", len(text), padded.size / rate)
        return padded.tobytes(), rate

    def synthesize_wav(self, text: str) -> bytes:
        """Synthesize one sentence into complete WAV bytes (correct rate, no clipping, padded)."""
        pcm, rate = self.synthesize_pcm(text)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        return buf.getvalue()
