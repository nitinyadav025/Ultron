"""Phase 5B: mic level repair.

The old normaliser used the single loudest sample, so one click (or the mic wake-up pop)
turned the whole recording down and quiet room noise was boosted up to 8x. These tests
pin the new rules: robust level, boost at most 3x, never boost silence, never clip,
and drop the clipped burst at the very start.
"""

from __future__ import annotations

import array
import wave
from pathlib import Path

import pytest

from assistant.config import load_config
from assistant.interfaces.voice import VoiceController, compute_pcm16_peak_rms
from assistant.speech.recorder import (
    SAFE_OUTPUT_PEAK,
    SyntheticSource,
    normalise_mic_level,
    trim_leading_transient,
)

FRAME = 320


def _tone(amp: int, frames: int) -> bytes:
    """Square wave at ``amp`` (rms == peak == amp) for the given number of 20 ms frames."""
    return array.array("h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]).tobytes()


def _speech_like(level: int, cycles: int = 10) -> bytes:
    """200 ms of 'voice' (10 loud frames) then 80 ms of quiet room noise, repeated."""
    quiet = max(1, level // 20)
    return b"".join(_tone(level, 10) + _tone(quiet, 4) for _ in range(cycles))


# --------------------------------------------------------------------------------------
# normalise_mic_level (unit)
# --------------------------------------------------------------------------------------


def test_click_only_applies_the_small_safety_attenuation() -> None:
    pcm = _tone(32767, 1) + _speech_like(3000)

    out, info = normalise_mic_level(pcm, has_speech=True)

    # A true full-scale sample now caps the gain at 0.9 (the old code used 0.7 for the
    # whole recording). The speech level itself is untouched.
    assert info.gain == pytest.approx(0.9, abs=0.01)
    assert info.after_peak <= 29500
    assert info.speech_level == 3000
    assert compute_pcm16_peak_rms(out)[0] <= 29500


def test_quiet_speech_is_boosted_no_more_than_three_times() -> None:
    pcm = _tone(32767, 0) + _speech_like(100)

    _out, info = normalise_mic_level(pcm, has_speech=True)

    assert info.gain == 3.0
    assert info.after_peak == 300


def test_silence_is_never_boosted() -> None:
    pcm = _tone(400, 50)

    out, info = normalise_mic_level(pcm, has_speech=False)

    assert info.gain == 1.0
    assert out == pcm
    assert info.before_peak == info.after_peak


def test_loud_but_not_clipping_speech_is_not_turned_down() -> None:
    pcm = _speech_like(20000)

    _out, info = normalise_mic_level(pcm, has_speech=True)

    # The new rule: only input already above 0.9 full scale is attenuated.
    assert info.gain == 1.0
    assert info.after_peak == 20000
    assert info.after_peak <= SAFE_OUTPUT_PEAK


@pytest.mark.parametrize(
    ("peak", "expected_gain", "expected_after"),
    [
        (9630, 3.0, 28890),
        (21189, 29490 / 21189, 29490),
        (27616, 29490 / 27616, 29490),
        (32768, 29490 / 32768, 29490),
        (3000, 3.0, 9000),
    ],
)
def test_normaliser_never_exceeds_the_safe_peak(peak: int, expected_gain: float, expected_after: int) -> None:
    # Speech-like body at RMS 973 (the live "speech level 973" case) plus one true peak.
    samples = array.array("h", [973 if i % 2 == 0 else -973 for _ in range(60) for i in range(FRAME)])
    if peak >= 32768:
        samples[100:200] = array.array("h", [-32768] * 100)  # a real clipped burst
    else:
        samples[100] = peak
    pcm = samples.tobytes()

    out, info = normalise_mic_level(pcm, has_speech=True)

    assert info.before_peak == peak
    assert info.gain == pytest.approx(expected_gain, rel=1e-3)
    assert info.after_peak == pytest.approx(expected_after, abs=2)
    assert info.after_peak <= 29500
    if peak <= SAFE_OUTPUT_PEAK:
        assert info.gain >= 1.0
        assert info.clipping is False
    else:
        assert info.gain < 1.0
        assert info.clipping is True
    assert compute_pcm16_peak_rms(out)[0] <= 29500


def test_boost_never_pushes_normal_speech_into_clipping() -> None:
    pcm = _speech_like(3000)

    _out, info = normalise_mic_level(pcm, has_speech=True)

    assert info.gain > 1.0
    assert info.after_peak <= SAFE_OUTPUT_PEAK


def test_many_full_scale_samples_are_reported_as_clipping() -> None:
    _out, info = normalise_mic_level(_tone(32767, 10), has_speech=False)

    assert info.clipping is True
    assert info.clipping_samples > 0


def test_empty_audio_is_returned_unchanged() -> None:
    out, info = normalise_mic_level(b"", has_speech=True)
    assert out == b"" and info.gain == 1.0


# --------------------------------------------------------------------------------------
# trim_leading_transient
# --------------------------------------------------------------------------------------


def test_leading_wake_up_pop_is_trimmed() -> None:
    pcm = _tone(32767, 15) + _speech_like(3000)  # 300 ms of full-scale thump, then speech

    trimmed, dropped_ms = trim_leading_transient(pcm)

    assert dropped_ms == 300
    assert len(trimmed) < len(pcm)
    peak, _ = compute_pcm16_peak_rms(trimmed)
    assert peak < 31000


def test_clean_speech_at_the_start_is_never_trimmed() -> None:
    pcm = _speech_like(6000)

    trimmed, dropped_ms = trim_leading_transient(pcm)

    assert dropped_ms == 0
    assert trimmed == pcm


def test_pop_far_from_the_start_is_not_trimmed() -> None:
    pcm = _speech_like(3000, cycles=10) + _tone(32767, 5)

    _trimmed, dropped_ms = trim_leading_transient(pcm)

    assert dropped_ms == 0


# --------------------------------------------------------------------------------------
# record_push_to_talk integration (fake pw-record, no microphone)
# --------------------------------------------------------------------------------------


def _fake_source(monkeypatch: pytest.MonkeyPatch, pcm: bytes) -> SyntheticSource:
    """Fixed mode now uses the same streaming source auto mode uses."""
    source = SyntheticSource(pcm)
    monkeypatch.setattr("assistant.interfaces.voice.PwRecordSource", lambda: source)
    return source


def _controller(tmp_path: Path) -> VoiceController:
    cfg = load_config()
    cfg.backend = "local"
    voice = VoiceController(config=cfg, adapter=None)
    voice.audio_file = tmp_path / "mic.wav"
    voice.debug_dir = tmp_path / "voice_debug"
    return voice


def test_clipped_start_is_dropped_before_normalising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    _fake_source(monkeypatch, _tone(32767, 15) + _speech_like(3000))

    ok, detail = voice.record_push_to_talk(0.6)

    assert ok is True and detail == str(voice.audio_file)
    assert voice.last_peak_before < 31000  # the pop never reaches the level measurement
    with wave.open(str(voice.audio_file), "rb") as wf:
        peak, _ = compute_pcm16_peak_rms(wf.readframes(wf.getnframes()))
    assert peak < 31000
    assert peak > 3000  # real speech is still there


def test_quiet_speech_is_boosted_for_whisper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    _fake_source(monkeypatch, _speech_like(600))

    ok, _ = voice.record_push_to_talk(0.6)

    assert ok is True
    assert voice.last_peak_before == 600
    assert voice.last_peak == 1800  # exactly 3x, the cap


def test_room_noise_is_not_amplified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    _fake_source(monkeypatch, _tone(400, 50))

    ok, _ = voice.record_push_to_talk(0.6)

    assert ok is True
    assert voice.last_peak == 400  # gain 1.0: hiss stays hiss


def test_fixed_mode_keeps_the_old_window_behaviour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    voice.config.voice_mode = "fixed"
    voice.config.voice_fixed_seconds = 0.6
    _fake_source(monkeypatch, _speech_like(2000))

    ok, _ = voice.record()

    assert ok is True
    assert voice.config.voice_mode == "fixed"
