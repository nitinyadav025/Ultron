"""Part 5: wake-train output and the learned-alias safety rule."""

from __future__ import annotations

import array
import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from assistant.config import load_config
from assistant.interfaces.listen import HandsFreeListener, Transcript
from assistant.interfaces.wake_train import run_wake_train
from assistant.speech.recorder import FRAME_BYTES, FRAME_SAMPLES
from assistant.speech.wake import aliases_for, load_learned_aliases, wake_match

FRAME = FRAME_SAMPLES
LEARNED = ['outron', "i'm drawn", "i draw", "i'll do it", "i'll turn"]


def tone(frames: int, amp: int = 3000) -> bytes:
    return array.array("h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]).tobytes()


def silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def utterance(text: str = "") -> bytes:
    return silence(32) + tone(20) + silence(150)


def frames_of(pcm: bytes) -> list[bytes]:
    return [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]


class ScriptedSource:
    def __init__(self, pcm: bytes) -> None:
        self.queue: deque[bytes] = deque(frames_of(pcm))
        self.during_speech: deque[bytes] = deque()
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        if timeout == 0:
            return self.during_speech.popleft() if self.during_speech else b""
        return self.queue.popleft() if self.queue else None

    def stop(self) -> None:
        self.stopped = True


class ScriptedTranscriber:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)

    def __call__(self, _pcm: bytes) -> Transcript:
        return Transcript(True, self.texts.pop(0)) if self.texts else Transcript(True, "")


def _listener(text: str, *, learned: list[str] = LEARNED):
    cfg = load_config()
    cfg.backend = "local"
    cfg.wake_word = "ultron"
    cfg.wake_aliases = ""
    cfg.listen_early_check_ms = 800
    cfg.listen_after_speech_ms = 300
    cfg.recorder_warmup_ms = 300
    commands: list[str] = []
    spoken: list[str] = []
    prints: list[str] = []
    listener = HandsFreeListener(
        cfg,
        ScriptedSource(utterance()),
        transcribe=ScriptedTranscriber([text]),
        speak=spoken.append,
        command_runner=lambda command: (commands.append(command) or ("Done.", False)),
        print_fn=prints.append,
    )
    listener.learned_aliases = learned
    return listener, commands, spoken, prints


# -- learned alias file ----------------------------------------------------------------


def test_learned_aliases_file_formats(tmp_path: Path) -> None:
    path = tmp_path / "wake_aliases.json"
    path.write_text(json.dumps({"aliases": {"ultron": 12, "i'll turn": 3}}), encoding="utf-8")
    assert load_learned_aliases(path) == ["ultron", "i'll turn"]

    path.write_text(json.dumps({"aliases": ["outron", "i draw"]}), encoding="utf-8")
    assert load_learned_aliases(path) == ["outron", "i draw"]

    assert load_learned_aliases(tmp_path / "missing.json") == []
    path.write_text("not json", encoding="utf-8")
    assert load_learned_aliases(path) == []


def test_matching_uses_learned_aliases_up_to_three_words() -> None:
    match = wake_match("I'll turn open firefox", "ultron", "", LEARNED)
    assert match.found and match.learned
    assert match.command == "open firefox"
    assert "outron" in aliases_for("ultron", "", LEARNED)


# -- safety rule -----------------------------------------------------------------------


@pytest.mark.parametrize("alias", LEARNED)
def test_learned_alias_with_a_fast_path_remainder_runs(alias: str) -> None:
    listener, commands, _spoken, _prints = _listener(f"{alias} open firefox")

    listener.run()

    assert commands == ["open firefox"]


@pytest.mark.parametrize("alias", LEARNED)
def test_learned_alias_alone_opens_the_window(alias: str) -> None:
    listener, commands, spoken, _prints = _listener(alias)

    listener.run()

    assert commands == []
    assert spoken == ["Yes?"]


@pytest.mark.parametrize("alias", [name for name in LEARNED if name != "outron"])
def test_learned_alias_with_unknown_text_is_ignored(alias: str) -> None:
    # "outron" is a built-in wake alias now, so anything after it is a real command.
    listener, commands, spoken, _prints = _listener(f"{alias} what is the weather")

    listener.run()

    assert commands == []
    assert spoken == []
    assert listener.ignored == 1


@pytest.mark.parametrize(
    "text",
    ["I think it will rain tomorrow", "I'll turn on the lights", "out of the way", "draw a picture"],
)
def test_ordinary_sentences_are_ignored(text: str) -> None:
    listener, commands, spoken, _prints = _listener(text)

    listener.run()

    assert commands == [] and spoken == []
    assert listener.ignored == 1


# -- wake-train ------------------------------------------------------------------------


class FakeEars:
    def __init__(self, text: str = "Ultron") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def transcribe_samples(self, _samples: Any, **kwargs: Any) -> tuple[bool, str]:
        self.calls.append(kwargs)
        return True, self.text


def test_wake_train_saves_clips_and_writes_alias_counts(tmp_path: Path) -> None:
    pcm = tone(20) + silence(40)
    cues: list[bytes] = [pcm] * 16
    lines: list[str] = []
    ears = FakeEars("Ultron")
    clips = tmp_path / "wake_clips"
    aliases = tmp_path / "wake_aliases.json"

    code = run_wake_train(
        load_config(),
        capture=lambda: cues.pop(0) if cues else None,
        ears=ears,
        small_ears=None,
        prompt=lines.append,
        clips_dir=clips,
        aliases_path=aliases,
    )

    assert code == 0
    assert len(list(clips.glob("wake_*.wav"))) == 16
    payload = json.loads(aliases.read_text(encoding="utf-8"))
    assert payload["clips"] == 16
    assert payload["aliases"]["ultron"] == 16
    assert any("average plain" in line for line in lines)
    assert any("No default was changed" in line for line in lines)
    # Three production variants per clip; small.en is not installed.
    assert len(ears.calls) == 16 * 3
    assert any(call.get("hotwords") == load_config().whisper_wake_hotwords for call in ears.calls)
    assert any(call.get("vad_filter") is False for call in ears.calls)
