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
from types import SimpleNamespace

import pytest

from assistant.config import load_config
from assistant.interfaces.voice import VoiceController, compute_pcm16_peak_rms
from assistant.speech.recorder import (
    SAFE_OUTPUT_PEAK,
    TARGET_SPEECH_RMS,
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


def test_click_does_not_turn_the_whole_recording_down() -> None:
    pcm = _tone(32767, 1) + _speech_like(3000)

    out, info = normalise_mic_level(pcm, has_speech=True)

    assert info.gain > 1.0  # old peak-based code used gain = 0.7 here
    assert info.after_rms > info.before_rms
    assert info.speech_level == 3000


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


def test_loud_speech_is_turned_down_to_a_healthy_level() -> None:
    pcm = _speech_like(20000)

    _out, info = normalise_mic_level(pcm, has_speech=True)

    assert info.gain < 1.0
    assert info.after_peak <= SAFE_OUTPUT_PEAK
    assert abs(info.after_peak - TARGET_SPEECH_RMS) <= 60


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


def _fake_recorder(monkeypatch: pytest.MonkeyPatch, wav_path: Path, pcm: bytes, rate: int = 16000) -> None:
    def fake_run() -> SimpleNamespace:
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        return SimpleNamespace(returncode=0, stderr="")

    class DoneProc:
        returncode = 0

        def poll(self) -> None:
            return None

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

        def send_signal(self, _sig: int) -> None:
            return None

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_popen(cmd: list[str], **_kwargs: object) -> object:
        fake_run()
        return DoneProc()

    monkeypatch.setattr(
        "assistant.interfaces.voice.shutil.which", lambda name: "pw-record" if name == "pw-record" else None
    )
    monkeypatch.setattr("assistant.interfaces.voice.subprocess.Popen", fake_popen)


def _controller(tmp_path: Path) -> VoiceController:
    cfg = load_config()
    cfg.backend = "local"
    voice = VoiceController(config=cfg, adapter=None)
    voice.audio_file = tmp_path / "mic.wav"
    return voice


def test_clipped_start_is_dropped_before_normalising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    _fake_recorder(monkeypatch, voice.audio_file, _tone(32767, 15) + _speech_like(3000))

    ok, detail = voice.record_push_to_talk(0.1)

    assert ok is True and detail == str(voice.audio_file)
    assert voice.last_peak_before < 31000  # the pop never reaches the level measurement
    with wave.open(str(voice.audio_file), "rb") as wf:
        peak, _ = compute_pcm16_peak_rms(wf.readframes(wf.getnframes()))
    assert peak < 31000
    assert peak > 3000  # real speech is still there


def test_quiet_speech_is_boosted_for_whisper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    _fake_recorder(monkeypatch, voice.audio_file, _speech_like(600))

    ok, _ = voice.record_push_to_talk(0.1)

    assert ok is True
    assert voice.last_peak_before == 600
    assert voice.last_peak == 1800  # exactly 3x, the cap


def test_room_noise_is_not_amplified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    _fake_recorder(monkeypatch, voice.audio_file, _tone(400, 50))

    ok, _ = voice.record_push_to_talk(0.1)

    assert ok is True
    assert voice.last_peak == 400  # gain 1.0: hiss stays hiss


def test_fixed_mode_keeps_the_old_window_behaviour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    voice.config.voice_mode = "fixed"
    voice.config.voice_fixed_seconds = 0.1
    _fake_recorder(monkeypatch, voice.audio_file, _speech_like(2000))

    ok, _ = voice.record()

    assert ok is True
    assert voice.config.voice_mode == "fixed"
