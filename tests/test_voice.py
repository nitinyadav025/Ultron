"""Offline unit tests for the Voice Pipeline (no network, no audio hardware)."""

from __future__ import annotations

import array
import io
import wave
from pathlib import Path
from types import SimpleNamespace

from assistant.config import load_config
from assistant.interfaces.voice import (
    TARGET_PEAK_MINUS_3DBFS,
    VoiceController,
    clean_speech_text,
    compute_pcm16_peak_rms,
    ensure_wav_bytes,
    scale_pcm16_bytes,
)


def _make_pcm16_sine_like(sample_val: int, count: int = 1600) -> bytes:
    arr = array.array("h", [sample_val if i % 2 == 0 else -sample_val for i in range(count)])
    return arr.tobytes()


def test_wav_detection_and_raw_pcm_wrapping() -> None:
    raw_pcm = _make_pcm16_sine_like(4000, count=2400)
    wrapped_wav = ensure_wav_bytes(raw_pcm, default_rate=24000, default_channels=1, normalize_to_minus_3dbfs=True, volume_gain=1.0)
    assert wrapped_wav.startswith(b"RIFF")

    with wave.open(io.BytesIO(wrapped_wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        frames = wf.readframes(wf.getnframes())

    peak, _ = compute_pcm16_peak_rms(frames)
    assert abs(peak - TARGET_PEAK_MINUS_3DBFS) <= 15

    # Passing an already-valid RIFF WAV preserves RIFF header and normalizes safely
    rewrapped = ensure_wav_bytes(wrapped_wav, normalize_to_minus_3dbfs=True, volume_gain=1.0)
    assert rewrapped.startswith(b"RIFF")


def test_peak_rms_and_clipping_protection() -> None:
    raw = _make_pcm16_sine_like(1000, count=1000)
    peak, rms = compute_pcm16_peak_rms(raw)
    assert peak == 1000
    assert 990 <= rms <= 1010

    boosted = scale_pcm16_bytes(raw, target_peak=TARGET_PEAK_MINUS_3DBFS, volume_gain=2.0)
    b_peak, _ = compute_pcm16_peak_rms(boosted)
    assert b_peak <= 32767


def test_clean_speech_text_strips_noise_and_limits_length() -> None:
    dirty = (
        '<untrusted_tool_output tool="read_file">{"secret": 123}</untrusted_tool_output>\n'
        "Here is what I found in `/home/nitin/Documents/report.txt`:\n"
        "```python\nprint('hello')\n```\n"
        "**Brave Browser** has been launched for you! "
        "I also checked `/etc/hosts` and verified the window is active. "
        "This third sentence should be dropped because only one or two sentences are spoken."
    )
    cleaned = clean_speech_text(dirty, max_chars=220)
    assert "untrusted_tool_output" not in cleaned
    assert "print(" not in cleaned
    assert "/home/nitin" not in cleaned
    assert "**" not in cleaned
    assert len(cleaned) <= 220
    assert "Brave Browser has been launched for you!" in cleaned


def test_transcription_fallback_order_and_no_speech(tmp_path: Path) -> None:
    cfg = load_config()
    cfg.backend = "gemini"  # cloud fallback chain only exists on the Gemini backend
    cfg.models["transcribe"] = "gemini-3.5-transcribe"
    cfg.models["fast"] = "gemini-3.5-flash-lite"
    cfg.models["main"] = "gemini-3.8-flash"

    wav_file = tmp_path / "sample.wav"
    wav_file.write_bytes(ensure_wav_bytes(_make_pcm16_sine_like(5000, 1600), default_rate=16000))

    called_models: list[str] = []

    def fake_generate_content(model: str, contents: object, config: object = None) -> object:
        called_models.append(model)
        if model == "gemini-3.5-transcribe":
            raise RuntimeError("Model does not support audio generateContent on free tier")
        if model == "gemini-3.5-flash-lite":
            return SimpleNamespace(text="")
        return SimpleNamespace(text="open brave")

    fake_adapter = SimpleNamespace(
        client=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content))
    )
    vc = VoiceController(config=cfg, adapter=fake_adapter)  # type: ignore[arg-type]
    ok, transcript = vc.transcribe_audio(wav_path=wav_file)

    assert ok is True
    assert transcript == "open brave"
    assert called_models == ["gemini-3.5-transcribe", "gemini-3.5-flash-lite", "gemini-3.8-flash"]

    # Verify [NO_SPEECH] returns "I didn't catch that" without unnecessary retries
    def fake_no_speech(model: str, contents: object, config: object = None) -> object:
        return SimpleNamespace(text="[NO_SPEECH]")

    fake_adapter_ns = SimpleNamespace(
        client=SimpleNamespace(models=SimpleNamespace(generate_content=fake_no_speech))
    )
    vc_ns = VoiceController(config=cfg, adapter=fake_adapter_ns)  # type: ignore[arg-type]
    ok_ns, msg_ns = vc_ns.transcribe_audio(wav_path=wav_file)
    assert ok_ns is False
    assert msg_ns == "I didn't catch that"
