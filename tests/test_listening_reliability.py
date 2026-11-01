"""Listening reliability: the nine live failure cases, replayed offline.

Real clips come from /tmp/cmp2_*.wav and the tests skip with a clear message when they are
missing. Every remaining case uses synthetic speech-like signals, so the suite never
depends on /tmp. No microphone is opened.
"""

from __future__ import annotations

import array
import json
import logging
import random
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest

from assistant.config import PROJECT_ROOT, load_config
from assistant.interfaces.voice import VoiceController
from assistant.speech.ears import WhisperEars
from assistant.speech.recorder import (
    FRAME_MS,
    FRAME_SAMPLES,
    Recording,
    SyntheticSource,
    frame_peak,
    frame_rms,
    pcm16_samples,
    record_until_silence,
    samples_to_pcm16,
    split_frames,
    write_pcm16_wav,
)

FRAME = FRAME_SAMPLES
EXPECTED = [
    "turn volume to thirty percent",
    "list files in my downloads folder",
    "open firefox",
    "mute",
    "open firefox and list my downloads",
    "turn the volume up",
]


def _tone(amp: int, frames: int) -> bytes:
    return array.array("h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]).tobytes()


def _silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def _noise(frames: int, amp: int = 90, seed: int = 7) -> bytes:
    rng = random.Random(seed)
    return array.array("h", [rng.randint(-amp, amp) for _ in range(frames * FRAME)]).tobytes()


def _duration_s(pcm: bytes) -> float:
    return len(pcm) / 2 / 16000


def _section_rms(pcm: bytes, start_ms: int, end_ms: int) -> int:
    samples = pcm16_samples(pcm)
    start = max(0, int(start_ms * 16))
    end = min(len(samples), int(end_ms * 16))
    section = samples[start:end]
    if not section:
        return 0
    return frame_rms(section)


def _first_loud_frame(pcm: bytes, threshold: int = 300) -> int:
    for index, frame in enumerate(split_frames(pcm)):
        if frame_rms(frame) >= threshold:
            return index
    return -1


def _loud_duration_s(pcm: bytes, threshold: int = 300) -> float:
    frames = split_frames(pcm)
    loud = [i for i, frame in enumerate(frames) if frame_rms(frame) >= threshold]
    if not loud:
        return 0.0
    return (loud[-1] - loud[0] + 1) * FRAME_MS / 1000.0


def _trim_silence(pcm: bytes, threshold: int = 300, keep_ms: int = 100) -> bytes:
    """Trim leading/trailing room noise so two clips can be joined with an exact pause."""
    frames = split_frames(pcm)
    loud = [i for i, frame in enumerate(frames) if frame_rms(frame) >= threshold]
    if not loud:
        return pcm
    keep = keep_ms // FRAME_MS
    return b"".join(samples_to_pcm16(frame) for frame in frames[max(0, loud[0] - keep) : loud[-1] + keep + 1])


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1
        return wf.readframes(wf.getnframes())


@dataclass(frozen=True)
class RealClip:
    pcm: bytes
    expected: str
    label: str


def _real_clips() -> list[RealClip]:
    """All real recordings available: cmp2 files, then live attempts with a good transcript.

    The original cmp2 clips are gone from this machine. The saved live attempts are used
    instead: each JSON trace says what Whisper heard; only clips whose transcript passed
    the guard (verdict "ok") are kept, so the expected text is measured, not guessed.
    """
    clips: list[RealClip] = []
    for index in range(6):
        for directory in (PROJECT_ROOT / "workspace" / "test_clips", Path("/tmp")):
            path = directory / f"cmp2_{index}.wav"
            if path.exists():
                clips.append(RealClip(_read_wav(path), EXPECTED[index], f"cmp2_{index}"))
                break
    for trace_path in sorted((PROJECT_ROOT / "workspace" / "test_clips").glob("attempt_*.json")):
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = (trace.get("transcript") or "").strip()
        verdict = (trace.get("guard") or {}).get("verdict")
        wav_path = trace_path.with_suffix(".wav")
        if verdict == "ok" and text and wav_path.exists():
            clips.append(RealClip(_read_wav(wav_path), text, trace_path.stem))
    return clips


def _require_clips() -> list[RealClip]:
    clips = _real_clips()
    if not clips:
        pytest.skip(
            "no real clips found: put cmp2_*.wav in workspace/test_clips/ (or /tmp), "
            "or save live attempts in workspace/test_clips/"
        )
    return clips


def _record(pcm: bytes, **kwargs: object) -> tuple[object, list[str]]:
    events: list[str] = []
    recording = record_until_silence(SyntheticSource(pcm), status=events.append, **kwargs)  # type: ignore[arg-type]
    return recording, events


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


@pytest.fixture(scope="module")
def ears() -> WhisperEars:
    cfg = load_config()
    cfg.backend = "local"
    probe = WhisperEars(cfg)
    if not probe.is_downloaded():
        pytest.skip("faster-whisper base.en model is not downloaded")
    loaded = WhisperEars(cfg)
    loaded.load()
    return loaded


def _transcribe(ears: WhisperEars, pcm: bytes, tmp_path: Path, tag: str) -> str:
    path = tmp_path / f"{tag}.wav"
    write_pcm16_wav(path, pcm, rate=16000, channels=1)
    ok, text = ears.transcribe(path)
    assert ok is True, f"guard rejected a real recording: {text!r}"
    return text


def _voice(tmp_path: Path) -> VoiceController:
    cfg = load_config()
    cfg.backend = "local"
    cfg.voice_start_timeout_s = 1.0
    cfg.voice_max_seconds = 10.0
    voice = VoiceController(config=cfg, adapter=None)
    voice.audio_file = tmp_path / "mic.wav"
    voice.debug_dir = tmp_path / "voice_debug"
    return voice


# 1) long sentence: real clip + 1 s noise before + 2.5 s after -------------------------


def test_all_real_clips_pass_the_guard(ears: WhisperEars, tmp_path: Path) -> None:
    clips = _require_clips()
    for clip in clips:
        path = tmp_path / f"guard_{clip.label}.wav"
        write_pcm16_wav(path, clip.pcm, rate=16000, channels=1)
        ok, text = ears.transcribe(path)
        assert ok is True, f"{clip.label} rejected: {text!r} ({ears.last_confidence})"
        assert ears.last_confidence["verdict"] == "ok"
        assert _norm(text) == _norm(clip.expected)


def test_long_sentence_is_kept_whole(ears: WhisperEars, tmp_path: Path) -> None:
    clips = _require_clips()
    clip = max(clips, key=lambda item: len(item.pcm))
    recording, _events = _record(_noise(25) + clip.pcm + _noise(125))

    assert recording.saw_speech is True and recording.reason == "silence"
    assert _duration_s(recording.pcm) >= _loud_duration_s(clip.pcm) - 0.3
    text = _transcribe(ears, recording.pcm, tmp_path, "long")
    assert _norm(text) == _norm(clip.expected)


def test_two_sentences_with_a_1500ms_pause_are_one_recording(ears: WhisperEars, tmp_path: Path) -> None:
    clips = _require_clips()
    first = next((c for c in clips if "download" in c.expected.lower()), clips[0])
    second = next((c for c in clips if "unmute" in c.expected.lower() and c is not first), None)
    if second is None:
        second = next((c for c in clips if c is not first), None)
    if second is None:
        pytest.skip("need two real clips to join; workspace/test_clips has only one")

    first_pcm = _trim_silence(first.pcm)
    second_pcm = _trim_silence(second.pcm)
    recording, _events = _record(_noise(25) + first_pcm + _silence(75) + second_pcm + _noise(125))

    assert recording.saw_speech is True and recording.reason == "silence"
    assert _duration_s(recording.pcm) >= _loud_duration_s(first_pcm) + _loud_duration_s(second_pcm)
    text = _norm(_transcribe(ears, recording.pcm, tmp_path, "two"))
    first_word = next(w for w in _norm(first.expected).split() if len(w) >= 4)
    second_word = next(w for w in _norm(second.expected).split() if len(w) >= 4)
    assert first_word in text and second_word in text


# 3) a 1.9 s pause inside a sentence (limit is 2.0 s) ----------------------------------


def test_a_1900ms_pause_does_not_end_the_recording() -> None:
    speech = _tone(3000, 60)  # 1.2 s
    pcm = _silence(25) + speech + _silence(95) + speech + _silence(125)  # 1.9 s pause

    recording, _events = _record(pcm, silence_ms=2000)

    assert recording.saw_speech is True and recording.reason == "silence"
    # If the pause had ended it, the saved audio would stop at ~2 s.
    assert _duration_s(recording.pcm) >= 4.6
    assert _section_rms(recording.pcm, 3300, 3800) > 1000  # the second half is there


# 4) soft start: first 0.8 s at 20% of the speaker's level ------------------------------


def test_soft_start_is_detected() -> None:
    soft = _tone(600, 40)  # 0.8 s, ~5x quieter than the 3000 rest
    loud = _tone(3000, 75)  # 1.5 s
    recording, _events = _record(_silence(25) + soft + loud + _silence(125))

    assert recording.saw_speech is True and recording.reason == "silence"
    assert _duration_s(recording.pcm) >= 2.3
    first = _first_loud_frame(recording.pcm)
    assert first >= 0
    assert 300 <= _section_rms(recording.pcm, first * FRAME_MS, first * FRAME_MS + 600) <= 1200


# 5) room noise before and after --------------------------------------------------------


def test_low_room_noise_does_not_block_detection() -> None:
    recording, _events = _record(_noise(50) + _tone(3000, 75) + _noise(125))

    assert recording.saw_speech is True and recording.reason == "silence"
    assert _duration_s(recording.pcm) >= 1.5
    assert recording.trace["thresholds"]["noise_floor"] >= 40
    assert recording.trace["thresholds"]["start"] > 0


# 6) a short click before speech --------------------------------------------------------


def test_short_click_before_speech_is_ignored() -> None:
    click = _tone(32000, 5)  # 100 ms, full scale, square (crest ~1)
    pcm = click + _silence(50) + _tone(3000, 50) + _silence(125)

    recording, _events = _record(pcm)

    assert recording.saw_speech is True
    # The click at 0-100 ms must not be the speech start; real speech starts at ~1.1 s.
    assert recording.trace["speech_start_ms"] >= 1000
    assert frame_peak(pcm16_samples(recording.pcm[:640])) < 31000


# 7) nothing speech-like for the full start timeout ------------------------------------


def test_only_noise_times_out_without_sending_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recording, _events = _record(_noise(75), start_timeout_s=1.0)
    assert recording.saw_speech is False
    assert recording.reason == "timeout"
    assert recording.pcm == b""

    voice = _voice(tmp_path)
    source = SyntheticSource(_noise(75))
    monkeypatch.setattr("assistant.interfaces.voice.PwRecordSource", lambda: source)
    monkeypatch.setattr(voice, "transcribe_audio", lambda *_a, **_k: pytest.fail("silence reached Whisper"))

    ok, message = voice.record_auto()

    assert ok is False and message == "I didn't hear anything."
    assert not voice.audio_file.exists()
    assert list((tmp_path / "voice_debug").glob("attempt_*.json"))


# 8) Enter finishes the capture ---------------------------------------------------------


def test_enter_mid_speech_ends_and_keeps_what_was_heard() -> None:
    state = {"polls": 0}

    def enter_after_two_seconds() -> bool:
        state["polls"] += 1
        return state["polls"] >= 125

    recording, events = _record(
        _silence(25) + _tone(3000, 300),  # 6 s of speech, no trailing silence
        enter_pressed=enter_after_two_seconds,
    )

    assert recording.saw_speech is True and recording.reason == "enter"
    assert _duration_s(recording.pcm) >= 1.8
    assert events.count("Listening... speak now (press Enter when done)") == 1


def test_enter_before_speech_cancels() -> None:
    recording, _events = _record(_silence(25) + _tone(3000, 300), enter_pressed=lambda: True)

    assert recording.saw_speech is False and recording.reason == "cancelled"
    assert recording.pcm == b""


# 9) fixed mode keeps the whole window --------------------------------------------------


def test_fixed_mode_records_the_full_window_without_losing_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = _voice(tmp_path)
    pcm = _tone(500, 25) + _tone(6000, 150)  # 0.5 s quiet-ish, then 3 s loud
    monkeypatch.setattr("assistant.interfaces.voice.PwRecordSource", lambda: SyntheticSource(pcm))

    ok, detail = voice.record_push_to_talk(2.5)

    assert ok is True and detail == str(voice.audio_file)
    with wave.open(str(voice.audio_file), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        data = wf.readframes(frames)
    assert abs(frames / rate - 2.5) <= 0.1
    # The last 0.5 s (the "later words") is still loud: the old file+SIGINT path lost it.
    assert _section_rms(data, 2000, 2500) > 3000


# Debug capture: transcript/scores, pruning, one log line -------------------------------


def test_debug_attempt_stores_transcript_and_guard_scores(tmp_path: Path) -> None:
    voice = _voice(tmp_path)
    recording = Recording(
        pcm=_tone(3000, 50),
        saw_speech=True,
        reason="silence",
        duration_ms=1000,
        speech_ms=800,
        trace={"thresholds": {"start": 280, "end": 150}, "levels": [{"t_ms": 100, "rms": 3000}]},
    )
    voice._save_debug_attempt(recording, "auto")

    class FakeEars:
        last_confidence = {
            "min_avg_logprob": -0.2,
            "max_no_speech_prob": 0.05,
            "max_compression_ratio": 0.7,
            "text": "mute",
            "verdict": "ok",
        }

    voice.ears = FakeEars()  # type: ignore[assignment]
    voice._attach_debug_transcript("mute")

    files = list((tmp_path / "voice_debug").glob("attempt_*.json"))
    assert len(files) == 1
    trace = json.loads(files[0].read_text(encoding="utf-8"))
    assert trace["transcript"] == "mute"
    assert trace["guard"]["verdict"] == "ok"
    assert trace["levels"][0]["rms"] == 3000


def test_only_the_newest_ten_debug_attempts_are_kept(tmp_path: Path) -> None:
    directory = tmp_path / "voice_debug"
    directory.mkdir()
    for index in range(12):
        stem = f"attempt_20260101-000000-{index:06d}"
        (directory / f"{stem}.wav").write_bytes(b"x")
        (directory / f"{stem}.json").write_text("{}", encoding="utf-8")

    VoiceController._prune_debug_attempts(directory, keep=10)

    assert len(list(directory.glob("attempt_*.json"))) == 10
    assert len(list(directory.glob("attempt_*.wav"))) == 10
    assert not (directory / "attempt_20260101-000000-000000.json").exists()


def test_each_attempt_logs_one_info_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ultron.voice")
    voice = _voice(tmp_path)
    recording = Recording(saw_speech=False, reason="timeout", duration_ms=1000, trace={"thresholds": {"start": 280}})

    voice._save_debug_attempt(recording, "auto")

    assert caplog.text.count("Listening attempt:") == 1


# --------------------------------------------------------------------------------------
# Bug A: steady noise (the room fan) must never count as speech
# --------------------------------------------------------------------------------------

FAN_TRACE = PROJECT_ROOT / "workspace" / "test_clips" / "attempt_20261101-135201-414426"


def _fan_pcm() -> bytes:
    path = FAN_TRACE.with_suffix(".wav")
    if not path.exists():
        pytest.skip("the 30 s fan recording is not in workspace/test_clips/")
    return _read_wav(path)


def _constant_noise(frames: int, amp: int = 4700, seed: int = 11) -> bytes:
    rng = random.Random(seed)
    return array.array("h", [rng.randint(-amp, amp) for _ in range(frames * FRAME)]).tobytes()


def test_constant_loud_noise_never_starts_a_recording() -> None:
    # A full-scale burst in the first 20 ms (the pw-record opening pop), then 30 s of
    # steady loud noise at the level of the live fan complaint.
    burst = _tone(32767, 1)
    recording, _events = _record(burst + _constant_noise(1500), max_seconds=30, start_timeout_s=2)

    assert recording.saw_speech is False
    assert recording.pcm == b""
    assert recording.reason in ("timeout", "source_ended")
    assert recording.trace["thresholds"]["min_snr_db"] == 10.0
    assert recording.trace["thresholds"]["warmup_ms"] == 300


def test_fan_only_recording_says_nothing_heard_and_never_reaches_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = _voice(tmp_path)
    voice.config.voice_start_timeout_s = 2.0
    monkeypatch.setattr("assistant.interfaces.voice.PwRecordSource", lambda: SyntheticSource(_fan_pcm()))
    monkeypatch.setattr(voice, "transcribe_audio", lambda *_a, **_k: pytest.fail("fan audio reached Whisper"))

    ok, message = voice.record_auto()

    assert ok is False
    assert message == "I didn't hear anything."
    assert not voice.audio_file.exists()


def test_a_real_clip_with_the_opening_burst_still_transcribes_fully(
    ears: WhisperEars, tmp_path: Path
) -> None:
    clips = _require_clips()
    clip = max(clips, key=lambda item: len(item.pcm))
    burst = _tone(32767, 2)  # 40 ms pop at stream open, inside the 300 ms warm-up
    recording, _events = _record(burst + _noise(25) + clip.pcm + _noise(125))

    assert recording.saw_speech is True and recording.reason == "silence"
    assert _duration_s(recording.pcm) >= _duration_s(clip.pcm) - 0.2
    text = _transcribe(ears, recording.pcm, tmp_path, "burst_plus_speech")
    assert _norm(text) == _norm(clip.expected)
