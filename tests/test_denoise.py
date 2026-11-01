"""Optional clean-up before Whisper: gentle, length-preserving, speech-only."""

from __future__ import annotations

import array
from pathlib import Path

import numpy as np
import pytest

from assistant.config import load_config
from assistant.interfaces.voice import VoiceController
from assistant.speech.denoise import MAX_STRENGTH, denoise_float32, denoise_pcm16

RATE = 16000


def _sine(freq: float, amp: float, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _band_power(samples: np.ndarray, freq: float, width: float = 25.0) -> float:
    spectrum = np.abs(np.fft.rfft(samples.astype(np.float64))) ** 2
    freqs = np.fft.rfftfreq(samples.size, 1 / RATE)
    return float(spectrum[(freqs >= freq - width) & (freqs <= freq + width)].sum())


def test_strength_zero_returns_the_input_unchanged() -> None:
    samples = _sine(440, 3000, 0.5)
    out = denoise_float32(samples, strength=0.0)
    assert out is samples or np.array_equal(out, samples)


def test_highpass_removes_rumble_and_keeps_the_speech_band() -> None:
    rumble = _sine(50, 8000)
    speech = _sine(1000, 3000)
    mixed = rumble + speech

    # strength 0 switches the whole step off, so use the smallest non-zero gate value.
    cleaned = denoise_float32(mixed, strength=0.01, highpass_hz=100)

    assert _band_power(cleaned, 50) < _band_power(mixed, 50) / 4
    assert _band_power(cleaned, 1000) > _band_power(mixed, 1000) * 0.5


def test_gate_lowers_steady_hiss_between_words() -> None:
    rng = np.random.default_rng(3)
    hiss = (rng.normal(0, 1500, RATE).astype(np.float32))
    voice = _sine(500, 9000, 1.0)
    voice[: int(0.4 * RATE)] = 0  # a quiet gap before the word
    mixed = hiss + voice

    cleaned = denoise_float32(mixed, strength=MAX_STRENGTH, highpass_hz=100)

    gap = slice(int(0.1 * RATE), int(0.3 * RATE))
    assert float(np.sqrt(np.mean(cleaned[gap] ** 2))) < 0.75 * float(np.sqrt(np.mean(mixed[gap] ** 2)))


def test_length_is_preserved_and_output_never_gets_louder() -> None:
    samples = np.clip(_sine(300, 20000, 0.7), -1.0, 1.0)
    cleaned = denoise_float32(samples, strength=0.6)
    assert cleaned.size == samples.size
    assert cleaned.dtype == np.float32
    # Overlap-add leaves a small edge ripple; the gate never amplifies the signal.
    assert float(np.abs(cleaned).max()) <= 1.25 * float(np.abs(samples).max())


def test_pcm16_round_trip_keeps_the_byte_length() -> None:
    samples = _sine(700, 5000, 0.3)
    pcm = array.array("h", (int(v) for v in samples)).tobytes()
    out = denoise_pcm16(pcm, strength=0.6)
    assert len(out) == len(pcm)
    assert len(out) % 2 == 0


def test_audio_without_speech_is_never_denoised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.backend = "local"
    cfg.voice_denoise_strength = 0.6
    voice = VoiceController(config=cfg, adapter=None)
    voice.audio_file = tmp_path / "mic.wav"
    monkeypatch.setattr(
        "assistant.interfaces.voice.denoise_pcm16",
        lambda *_a, **_k: pytest.fail("denoise must not run on audio without speech"),
    )

    ok, _detail = voice._finish_recording(b"\x00\x00" * 800, 16000, 1, has_speech=False)

    assert ok is True
