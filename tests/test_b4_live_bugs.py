"""Phase 5 B4: deterministic replays of every bug from the Nov 1 live test.

No microphone, speaker, browser or real process is touched. The only real model use is
the saved wake clips (skipped when the model or clips are missing), which proves the
hotword + alias pipeline on the user's own voice.
"""

from __future__ import annotations

import array
import random
import subprocess
import wave
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from assistant.brain.fast_path import match_fast_command, try_fast_path
from assistant.brain.tool_replies import close_app_reply, open_app_reply
from assistant.config import PROJECT_ROOT, load_config
from assistant.interfaces import cli
from assistant.interfaces.listen import HandsFreeListener, Transcript, UtteranceDetector
from assistant.safety.permissions import PermissionGate
from assistant.speech.ears import UNCLEAR_SPEECH, WhisperEars
from assistant.speech.recorder import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    SyntheticSource,
    record_until_silence,
    split_frames,
)
from assistant.speech.wake import strip_wake_prefixes, wake_match
from assistant.tools import apps
from assistant.tools.apps import close_app
from assistant.tools.registry import ToolRegistry, tool

FRAME = FRAME_SAMPLES


# --------------------------------------------------------------------------------------
# Part 1: sub-second endpointing on the user's real room noise
# --------------------------------------------------------------------------------------


def _gauss(ms: int, sigma: float, seed: int) -> bytes:
    rng = random.Random(seed)
    values = [max(-32768, min(32767, int(rng.gauss(0, sigma)))) for _ in range(ms * 16)]
    return array.array("h", values).tobytes()


def test_recorder_stops_900ms_after_speech_despite_room_noise_450() -> None:
    # The live numbers: floor ~100, speech 1400-2500, post-speech room/breathing 200-450.
    pcm = _gauss(500, 100, 1) + _gauss(2000, 1800, 2) + _gauss(4000, 450, 3)

    recording = record_until_silence(SyntheticSource(pcm))

    assert recording.saw_speech is True
    assert recording.reason == "silence"  # never max_length / 30 s
    after_speech_ms = recording.duration_ms - 2500
    assert 800 <= after_speech_ms <= 1150
    assert recording.duration_ms < 5000
    assert recording.trace["thresholds"]["speech_rms"] > 1500


def test_detector_stops_900ms_after_speech_despite_room_noise_450() -> None:
    pcm = _gauss(500, 100, 1) + _gauss(2000, 1800, 2) + _gauss(4000, 450, 3)
    detector = UtteranceDetector(
        warmup_ms=300, silence_ms=900, early_ms=600, min_snr_db=10.0, max_seconds=30.0
    )
    events: list[str] = []
    final_ms = 0
    for frame in split_frames(pcm):
        event = detector.feed(frame)
        if event:
            events.append(event)
        if event == "final":
            final_ms = detector.audio_ms
            break

    assert "early" in events  # 600 ms early check = the snappy path
    assert "final" in events
    assert detector.last_reason == "silence"
    assert 3200 <= final_ms <= 3700  # speech ended at 2.5 s; +900 ms + pre-roll
    assert final_ms < 5000


# --------------------------------------------------------------------------------------
# Part 2: wake word on the user's 16 saved clips + short/duplicate handling
# --------------------------------------------------------------------------------------


def _wake_pipeline(text: str) -> tuple[bool, str]:
    match = wake_match(text)
    if not match.found:
        return False, ""
    command = strip_wake_prefixes(match.command)
    if match.learned and command and match_fast_command(command) is None:
        return False, ""
    return True, command


def _saved_wake_clips() -> list[Path]:
    folder = PROJECT_ROOT / "workspace" / "wake_clips"
    if not folder.exists():
        return []
    files = sorted(folder.glob("wake_20261101-1[78]*.wav"))
    return [path for path in files if path.name >= "wake_20261101-175835"]


def _wav_samples(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def test_saved_wake_clips_improve_with_hotwords_and_aliases() -> None:
    clips = _saved_wake_clips()
    if len(clips) < 16:
        pytest.skip("the 16 wake clips are not in workspace/wake_clips/")
    config = load_config()
    config.backend = "local"
    ears = WhisperEars(config)
    if not ears.is_downloaded():
        pytest.skip("faster-whisper base.en is not downloaded")
    ears.load()

    hot_texts: list[str] = []
    for path in clips[:16]:
        ears.transcribe_samples(
            _wav_samples(path), hotwords=config.whisper_wake_hotwords, log_text=False
        )
        hot_texts.append(str(ears.last_confidence.get("text") or ""))

    hot_single = sum(1 for text in hot_texts[:8] if _wake_pipeline(text)[0] and not _wake_pipeline(text)[1])
    hot_commands = sum(1 for text in hot_texts[8:12] if _wake_pipeline(text)[1] == "open firefox")

    plain_single = 0
    for path in clips[:8]:
        ears.transcribe_samples(_wav_samples(path), hotwords="", log_text=False)
        text = str(ears.last_confidence.get("text") or "")
        if _wake_pipeline(text)[0] and not _wake_pipeline(text)[1]:
            plain_single += 1

    # Measured on this machine: plain 4/8, hotwords 8/8; commands 4/4 with hotwords.
    assert hot_single >= 7
    assert hot_single >= plain_single
    assert hot_commands >= 3
    assert all(len(text) < 203 for text in hot_texts)  # no old word-list leak


class _WakeSource:
    def __init__(self, pcm: bytes) -> None:
        self.queue: deque[bytes] = deque(
            [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
        )
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        if timeout == 0:
            return b""
        return self.queue.popleft() if self.queue else None

    def stop(self) -> None:
        self.stopped = True


def _short_utterance() -> bytes:
    # 640 ms quiet lead-in (past the adaptive warm-up), 600 ms of voice, 1.2 s silence.
    return b"\x00\x00" * FRAME * 32 + array.array(
        "h", [3000 if i % 2 == 0 else -3000 for _ in range(30) for i in range(FRAME)]
    ).tobytes() + b"\x00\x00" * FRAME * 60


def _utterance() -> bytes:
    return b"\x00\x00" * FRAME * 32 + array.array(
        "h", [3000 if i % 2 == 0 else -3000 for _ in range(20) for i in range(FRAME)]
    ).tobytes() + b"\x00\x00" * FRAME * 150


def _listener(
    pcm: bytes,
    transcript: Transcript,
    *,
    debug: bool = False,
) -> tuple[HandsFreeListener, list[str], list[str]]:
    config = load_config()
    config.backend = "local"
    config.listen_early_check_ms = 600
    config.listen_after_speech_ms = 300
    config.voice_silence_ms = 900
    config.recorder_warmup_ms = 300
    config.voice_min_snr_db = 10
    config.listen_debug = debug
    spoken: list[str] = []
    prints: list[str] = []
    listener = HandsFreeListener(
        config,
        _WakeSource(pcm),
        transcribe=lambda _pcm: transcript,
        speak=spoken.append,
        command_runner=lambda command: (f"ran {command}", False),
        print_fn=prints.append,
    )
    return listener, spoken, prints


def test_repeated_wake_word_is_accepted_as_wake_only() -> None:
    # Real decoder output from the user's clips: "Ultron, Ultron, Ultron" was rejected
    # by the repetition guard and the wake word was lost.
    transcript = Transcript(
        False, UNCLEAR_SPEECH, raw="Ultron, Ultron, Ultron", reason="repetition_loop"
    )
    listener, spoken, _prints = _listener(_utterance(), transcript)

    listener.run()

    assert spoken == ["Yes?"]


def test_short_ultron_at_minus_1_2_logprob_is_still_the_wake_word() -> None:
    transcript = Transcript(
        False, UNCLEAR_SPEECH, raw="Ultron", reason="avg_logprob=-1.20"
    )
    listener, spoken, _prints = _listener(_short_utterance(), transcript)

    listener.run()

    assert spoken == ["Yes?"]


# --------------------------------------------------------------------------------------
# Part 3: first link, app validation, close_app
# --------------------------------------------------------------------------------------


class _Registry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

        @tool(name="open_app", risk_level="safe", read_only=False)
        def fake_open(app_name: str) -> dict[str, Any]:
            """Open an app."""
            self.calls.append(("open_app", {"app_name": app_name}))
            return {"ok": True, "app_name": app_name}

        config = load_config()
        gate = PermissionGate(config=config, mode="auto-safe")
        self.registry = ToolRegistry(config=config, gate=gate)
        self.registry.register(fake_open)


def _fast_reply(text: str) -> tuple[str | None, list[tuple[str, dict[str, Any]]]]:
    holder = _Registry()
    return try_fast_path(holder.registry, text), holder.calls


def test_first_link_routes_to_open_first_result_not_open_app() -> None:
    command = match_fast_command("Open the first link for the GitHub.")
    assert command is not None
    assert command["kind"] == "open_first_result"
    assert command["tool"] == "open_first_result"
    assert command["arguments"] == {"query": "github"}
    assert command["tool"] != "open_app"


def test_fake_app_is_never_claimed_open() -> None:
    reply = open_app_reply(
        {"ok": True, "raw_summary": {"ok": False, "unknown": True, "app_name": "banana spaceship app"}}
    )
    assert reply == "I don't know how to open banana spaceship app."


def test_wake_mishearing_extracts_open_firefox_and_runs_open_app() -> None:
    reply, calls = _fast_reply("open firefox")
    assert reply == "Firefox is open."
    assert calls == [("open_app", {"app_name": "firefox"})]

    for text in ("I'll draw an open Firefox.", "Ultron open 5ox"):
        match = wake_match(text)
        command = strip_wake_prefixes(match.command)
        assert command == "open firefox"


def _completed(argv: list[str], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def test_close_app_uses_pgrep_then_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "pgrep" and argv[-1] == "firefox":
            return _completed(argv, 0, "4242\n")
        if argv[0] == "pgrep":
            return _completed(argv, 1)
        return _completed(argv, 0)

    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(apps, "_process_runner", runner)

    result = close_app("firefox")

    assert result["ok"] is True and result["closed"] is True
    assert result["app_name"] == "Firefox" and result["pids"] == [4242]
    assert calls == [
        ["pgrep", "-x", "firefox"],
        ["pgrep", "-x", "firefox-bin"],
        ["kill", "-TERM", "4242"],
    ]
    assert close_app_reply({"ok": True, "raw_summary": result}) == "Closed Firefox."


def test_close_app_reports_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(apps, "_process_runner", lambda argv: _completed(argv, 1))

    result = close_app("firefox")

    assert result["ok"] is True and result["running"] is False
    assert close_app_reply({"ok": True, "raw_summary": result}) == "Firefox is not running."


def test_close_app_refuses_unknown_and_system_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")

    def explode(_argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("no process lookup for an unknown/system app")

    monkeypatch.setattr(apps, "_process_runner", explode)

    assert close_app("gnome-shell")["unknown"] is True
    assert close_app("banana spaceship")["unknown"] is True
    assert close_app_reply({"ok": True, "raw_summary": close_app("gnome-shell")}) == (
        "I don't know how to close gnome shell."
    )


# --------------------------------------------------------------------------------------
# Part 4: every /voice reply is spoken + LISTEN_DEBUG
# --------------------------------------------------------------------------------------


class _FakeVoice:
    ears = None
    mouth = None  # nothing to load; speak_sync is what we assert
    last_peak_before = 0
    last_rms_before = 0
    last_peak = 0
    last_rms = 0

    def __init__(self, record_result: tuple[bool, str], transcript: tuple[bool, str]) -> None:
        self.record_result = record_result
        self.transcript = transcript
        self.spoken: list[str] = []

    def record(self) -> tuple[bool, str]:
        return self.record_result

    def transcribe_audio(self) -> tuple[bool, str]:
        return self.transcript

    def speak_sync(self, text: str) -> tuple[bool, str]:
        self.spoken.append(text)
        return True, "Piper (en_US-lessac-medium)"

    def stop_speaking(self) -> None:
        return None


class _UnusedLoop:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def run_task(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("fast path and no-hear turns must not reach the LLM")


def _run_cli(monkeypatch: pytest.MonkeyPatch, voice: _FakeVoice, inputs: list[str]) -> None:
    monkeypatch.setattr(cli, "show_privacy_notice_once", lambda _config: None)
    monkeypatch.setattr(cli, "register_gnome_kill_hotkey", lambda _binding: None)
    monkeypatch.setattr(cli, "apply_mic_source_volume", lambda _value: (False, ""))
    monkeypatch.setattr(cli, "build_adapter", lambda _config: object())
    monkeypatch.setattr(cli, "VoiceController", lambda **_kwargs: voice)
    monkeypatch.setattr(cli, "AgentLoop", _UnusedLoop)
    stream = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(stream))
    assert cli.run_cli_chat(load_config()) == 0


def test_voice_fast_path_reply_is_spoken(monkeypatch: pytest.MonkeyPatch) -> None:
    voice = _FakeVoice((True, "fake.wav"), (True, "open firefox"))
    monkeypatch.setattr(cli, "try_fast_path", lambda _registry, _text: "Firefox is open.")

    _run_cli(monkeypatch, voice, ["/voice", "/quit"])

    assert voice.spoken == ["Firefox is open."]


def test_voice_consecutive_nothing_heard_is_spoken_each_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from assistant.interfaces.voice import NOTHING_HEARD

    voice = _FakeVoice((False, NOTHING_HEARD), (True, ""))

    _run_cli(monkeypatch, voice, ["/voice", "/voice", "/quit"])

    assert voice.spoken == [NOTHING_HEARD, NOTHING_HEARD]


def test_listen_debug_env_var_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LISTEN_DEBUG", "1")
    config = load_config(
        env_path=tmp_path / "missing.env", settings_path=tmp_path / "missing.yaml"
    )
    assert config.listen_debug is True


def test_listen_debug_prints_one_line_per_utterance() -> None:
    pcm = _utterance() + _utterance()
    texts = iter(["Ultron", "open firefox"])
    config = load_config()
    config.backend = "local"
    config.listen_early_check_ms = 600
    config.listen_after_speech_ms = 300
    config.voice_silence_ms = 900
    config.recorder_warmup_ms = 300
    config.voice_min_snr_db = 10
    config.listen_debug = True
    prints: list[str] = []
    spoken: list[str] = []
    listener = HandsFreeListener(
        config,
        _WakeSource(pcm),
        transcribe=lambda _pcm: Transcript(True, next(texts, "")),
        speak=spoken.append,
        command_runner=lambda command: (f"ran {command}", False),
        print_fn=prints.append,
    )

    listener.run()

    debug_lines = [line for line in prints if "debug: heard=" in line]
    assert len(debug_lines) == 2
    assert all("verdict=" in line and "wake_match=" in line and "score=" in line and "cmd=" in line for line in debug_lines)
