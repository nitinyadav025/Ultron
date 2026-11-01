"""Phase 5B: "listen until I stop talking" (VOICE_MODE=auto).

Every test feeds synthetic PCM chunks through the same code path the microphone uses,
so the silence detection, the pre-roll, the click filter and the hard cap are all proven
without a real mic.
"""

from __future__ import annotations

import array
from pathlib import Path

import pytest

from assistant.config import load_config
from assistant.interfaces.voice import VoiceController
from assistant.speech.recorder import (
    FRAME_MS,
    FRAME_SAMPLES,
    SyntheticSource,
    record_until_silence,
    split_frames,
)

FRAME = FRAME_SAMPLES


def _tone(amp: int, frames: int) -> bytes:
    return array.array("h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]).tobytes()


def _silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def _first_loud_frame(pcm: bytes, threshold: int = 1000) -> int:
    for index, frame in enumerate(split_frames(pcm)):
        if max(abs(v) for v in frame) >= threshold:
            return index
    return -1


def _record(pcm: bytes, **kwargs: object):
    events: list[str] = []
    source = SyntheticSource(pcm)
    recording = record_until_silence(source, status=events.append, **kwargs)  # type: ignore[arg-type]
    return recording, events, source


def test_waits_for_speech_and_keeps_pre_roll() -> None:
    pcm = _silence(50) + _tone(4000, 50) + _silence(150)

    recording, events, _source = _record(pcm)

    assert recording.saw_speech is True
    assert recording.reason == "silence"
    assert events == ["Listening... speak now (press Enter when done)", "Heard you...", "Got it."]
    # ~400 ms of silence is kept in front of the first loud frame (the first word survives).
    first = _first_loud_frame(recording.pcm)
    assert 15 <= first <= 25
    duration_s = len(recording.pcm) / 2 / 16000
    assert 1.4 <= duration_s <= 2.0


def test_short_click_is_not_speech() -> None:
    pcm = _silence(50) + _tone(32000, 5) + _silence(150)  # a 100 ms bump

    recording, events, _source = _record(pcm)

    assert recording.saw_speech is False
    assert recording.pcm == b""
    assert "Heard you..." not in events


def test_pause_inside_a_sentence_does_not_end_the_recording() -> None:
    pcm = _silence(20) + _tone(4000, 25) + _silence(25) + _tone(4000, 25) + _silence(100)

    recording, _events, _source = _record(pcm, silence_ms=1000)

    assert recording.saw_speech is True
    assert recording.reason == "silence"
    # both halves are there: 0.5 s + 0.5 s pause + 0.5 s of speech
    assert recording.duration_ms > 2000


def test_stops_after_the_configured_silence() -> None:
    pcm = _silence(20) + _tone(4000, 25) + _silence(300)

    recording, _events, _source = _record(pcm, silence_ms=600)

    assert recording.reason == "silence"
    # The 600 ms silence limit cannot stop before 1.5 s after speech began, so the stop
    # lands late; the saved audio still ends 300 ms after the last word.
    assert recording.duration_ms >= 1500
    duration_s = len(recording.pcm) / 2 / 16000
    assert 1.0 <= duration_s <= 1.4


def test_hard_cap_stops_a_never_ending_speaker() -> None:
    pcm = _silence(10) + _tone(4000, 400)  # 8 s of continuous "speech"

    recording, _events, _source = _record(pcm, max_seconds=3.0)

    assert recording.reason == "max_length"
    assert recording.saw_speech is True
    assert 2.9 <= recording.duration_ms / 1000 <= 3.3


def test_nothing_heard_within_timeout() -> None:
    pcm = _silence(150)  # 3 s of room noise

    recording, _events, source = _record(pcm, start_timeout_s=1.0)

    assert recording.saw_speech is False
    assert recording.reason == "timeout"
    assert recording.pcm == b""
    assert source.stopped is True


def test_device_wake_up_thump_is_not_speech() -> None:
    pcm = _tone(32767, 15) + _silence(100)  # 300 ms full-scale pop, then quiet

    recording, _events, _source = _record(pcm)

    assert recording.saw_speech is False


# --------------------------------------------------------------------------------------
# VoiceController.record_auto
# --------------------------------------------------------------------------------------


def _controller(tmp_path: Path) -> VoiceController:
    cfg = load_config()
    cfg.backend = "local"
    cfg.voice_mode = "auto"
    cfg.voice_start_timeout_s = 1.0
    cfg.voice_silence_ms = 600
    cfg.voice_max_seconds = 5.0
    voice = VoiceController(config=cfg, adapter=None)
    voice.audio_file = tmp_path / "mic.wav"
    voice.debug_dir = tmp_path / "voice_debug"
    return voice


def _patch_source(monkeypatch: pytest.MonkeyPatch, pcm: bytes) -> SyntheticSource:
    source = SyntheticSource(pcm)

    def factory() -> SyntheticSource:
        return source

    monkeypatch.setattr("assistant.interfaces.voice.PwRecordSource", factory)
    return source


def test_record_auto_saves_a_wav_when_speech_is_heard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = _controller(tmp_path)
    _patch_source(monkeypatch, _silence(20) + _speech() + _silence(100))

    ok, detail = voice.record_auto()

    assert ok is True
    assert detail == str(voice.audio_file)
    assert voice.audio_file.exists()
    assert voice.last_peak > 0
    assert voice.last_peak_before > 0


def _speech(frames: int = 40) -> bytes:
    return _tone(2500, frames)


def test_record_auto_returns_a_message_and_skips_whisper_when_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = _controller(tmp_path)
    _patch_source(monkeypatch, _silence(100))

    ok, detail = voice.record_auto()

    assert ok is False
    assert detail == "I didn't hear anything."
    assert not voice.audio_file.exists()  # nothing to transcribe


def test_ctrl_c_while_listening_only_cancels_the_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = _controller(tmp_path)
    calls = {"n": 0}

    def boom(_n: int) -> None:
        calls["n"] += 1
        raise KeyboardInterrupt

    source = SyntheticSource(_silence(100), on_read=boom)
    monkeypatch.setattr("assistant.interfaces.voice.PwRecordSource", lambda: source)

    ok, detail = voice.record_auto()

    assert ok is False
    assert detail == "cancelled"
    assert calls["n"] == 1


def test_never_records_while_piper_is_speaking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _controller(tmp_path)
    order: list[str] = []
    source = SyntheticSource(_silence(100) + _speech() + _silence(100))
    original_start = source.start

    def tracked_start() -> None:
        order.append("record")
        original_start()

    source.start = tracked_start  # type: ignore[method-assign]
    monkeypatch.setattr("assistant.interfaces.voice.PwRecordSource", lambda: source)
    monkeypatch.setattr(voice, "stop_speaking", lambda: order.append("stop_speaking"))
    monkeypatch.setattr(voice, "_wait_for_playback", lambda *_a, **_k: order.append("wait_playback"))

    voice.record_auto()

    assert order[:3] == ["stop_speaking", "wait_playback", "record"]


def test_frame_constants_match_20ms() -> None:
    assert FRAME_SAMPLES * 2 == 640
    assert FRAME_MS == 20
