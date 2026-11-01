"""Chat command parsing: slash lines never reach the LLM by accident."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from assistant.brain.agent_loop import AgentTaskResult
from assistant.config import load_config
from assistant.interfaces import cli
from assistant.interfaces.voice import NOTHING_HEARD
from assistant.speech.ears import UNCLEAR_SPEECH
from assistant.interfaces.cli import (
    UNKNOWN_COMMAND_MESSAGE,
    command_name,
    is_unknown_command,
    normalize_chat_line,
    run_cli_chat,
)


def test_double_slash_is_collapsed_and_trimmed() -> None:
    assert normalize_chat_line("  //voice  ") == "/voice"
    assert normalize_chat_line("///voice") == "/voice"
    assert normalize_chat_line("/voice now") == "/voice now"
    assert normalize_chat_line("hello //x") == "hello //x"
    assert normalize_chat_line("  hello  ") == "hello"


def test_command_name_and_unknown_detection() -> None:
    assert command_name("/voice now") == "/voice"
    assert command_name("hello") == ""
    assert is_unknown_command("/nonsense") is True
    assert is_unknown_command("/model 2") is False
    assert is_unknown_command("hello") is False
    assert UNKNOWN_COMMAND_MESSAGE == (
        "Unknown command. Available: /model, /thinking, /voice, /mode, /dry-run, /logs, /quit"
    )


class FakeVoice:
    ears = None
    mouth = None
    last_peak_before = 0
    last_rms_before = 0
    last_peak = 0
    last_rms = 0

    def __init__(self) -> None:
        self.record_calls = 0

    def record(self) -> tuple[bool, str]:
        self.record_calls += 1
        return True, "fake.wav"

    def transcribe_audio(self) -> tuple[bool, str]:
        return True, "tell me a story"

    def stop_speaking(self) -> None:
        return None

    def speak_sync(self, _text: str) -> tuple[bool, str]:
        return True, "fake"


class FakeLoop:
    instances: list["FakeLoop"] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls: list[str] = []
        FakeLoop.instances.append(self)

    def run_task(self, text: str, dry_run: bool = False) -> AgentTaskResult:
        self.calls.append(text)
        return AgentTaskResult(ok=True, reply="Done.", steps_taken=1)


def test_unknown_command_never_reaches_the_llm_and_double_slash_voice_works(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    FakeLoop.instances.clear()
    fake_voice = FakeVoice()
    inputs = iter(["//voice", "/nonsense", "/quit"])

    monkeypatch.setattr(cli, "show_privacy_notice_once", lambda _config: None)
    monkeypatch.setattr(cli, "register_gnome_kill_hotkey", lambda _binding: None)
    monkeypatch.setattr(cli, "apply_mic_source_volume", lambda _value: (False, ""))
    monkeypatch.setattr(cli, "build_adapter", lambda _config: object())
    monkeypatch.setattr(cli, "VoiceController", lambda **_kwargs: fake_voice)
    monkeypatch.setattr(cli, "AgentLoop", FakeLoop)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    caplog.set_level(logging.INFO, logger="ultron.cli")

    assert run_cli_chat(load_config()) == 0

    out = capsys.readouterr().out
    assert UNKNOWN_COMMAND_MESSAGE in out
    assert fake_voice.record_calls == 1  # //voice was treated as /voice
    assert FakeLoop.instances[0].calls == ["tell me a story"]  # /nonsense never reached run_task
    assert "Chat input: '//voice'" in caplog.text
    assert "Chat input: '/nonsense'" in caplog.text


class OutcomeVoice:
    """Voice double whose record/transcribe outcomes are chosen by the test."""

    ears = None
    mouth = None
    last_peak_before = 0
    last_rms_before = 0
    last_peak = 0
    last_rms = 0

    def __init__(self, record_outcome, transcribe_outcome=(True, "tell me a story")):
        self.record_outcome = record_outcome
        self.transcribe_outcome = transcribe_outcome
        self.spoken: list[str] = []
        self.record_calls = 0

    def record(self):
        self.record_calls += 1
        return self.record_outcome

    def transcribe_audio(self):
        return self.transcribe_outcome

    def speak_sync(self, text):
        self.spoken.append(text)
        return True, "fake"

    def stop_speaking(self):
        return None


def _run_with_voice(monkeypatch, voice, inputs):
    monkeypatch.setattr(cli, "show_privacy_notice_once", lambda _config: None)
    monkeypatch.setattr(cli, "register_gnome_kill_hotkey", lambda _binding: None)
    monkeypatch.setattr(cli, "apply_mic_source_volume", lambda _value: (False, ""))
    monkeypatch.setattr(cli, "build_adapter", lambda _config: object())
    monkeypatch.setattr(cli, "VoiceController", lambda **_kwargs: voice)
    monkeypatch.setattr(cli, "AgentLoop", FakeLoop)
    stream = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(stream))
    return run_cli_chat(load_config())


def test_nothing_heard_is_shown_and_spoken_once(monkeypatch, capsys) -> None:
    voice = OutcomeVoice((False, NOTHING_HEARD))

    assert _run_with_voice(monkeypatch, voice, ["/voice", "/quit"]) == 0

    out = capsys.readouterr().out
    assert f"Voice: {NOTHING_HEARD}" in out
    assert voice.spoken == [NOTHING_HEARD]


def test_guard_rejection_is_shown_and_spoken_once(monkeypatch, capsys) -> None:
    voice = OutcomeVoice((True, "fake.wav"), (False, UNCLEAR_SPEECH))

    assert _run_with_voice(monkeypatch, voice, ["/voice", "/quit"]) == 0

    out = capsys.readouterr().out
    assert f"Voice: {UNCLEAR_SPEECH}" in out
    assert voice.spoken == [UNCLEAR_SPEECH]


def test_tool_reply_is_spoken_once(monkeypatch) -> None:
    voice = OutcomeVoice((True, "fake.wav"))

    assert _run_with_voice(monkeypatch, voice, ["/voice", "/quit"]) == 0

    assert voice.spoken == ["Done."]
