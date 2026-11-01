"""Part 3 replay: what happens around "Ultron" -> "Yes?" -> a command.

Deterministic: audio "heard while Piper talks" goes through a separate queue, and the
delay after Piper is a number of 20 ms frames. The command clip is a real saved attempt.
"""

from __future__ import annotations

import array
import wave
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from assistant.config import PROJECT_ROOT, load_config
from assistant.interfaces.listen import HandsFreeListener, Transcript
from assistant.speech.recorder import FRAME_BYTES, FRAME_SAMPLES

FRAME = FRAME_SAMPLES
COMMAND_WAV = PROJECT_ROOT / "workspace" / "test_clips" / "attempt_20261101-134606-646872.wav"


def tone(frames: int, amp: int = 3000) -> bytes:
    return array.array("h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]).tobytes()


def silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def frames_of(pcm: bytes) -> list[bytes]:
    return [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]


def read_clip() -> bytes:
    if not COMMAND_WAV.exists():
        pytest.skip("real command clip is not in workspace/test_clips/")
    with wave.open(str(COMMAND_WAV), "rb") as wf:
        return wf.readframes(wf.getnframes())


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
    def __init__(self, responses: list[Transcript]) -> None:
        self.responses = list(responses)
        self.pcms: list[bytes] = []

    def __call__(self, pcm: bytes) -> Transcript:
        self.pcms.append(pcm)
        return self.responses.pop(0) if self.responses else Transcript(True, "")


def _listener(source: ScriptedSource, transcriber: Any, runner: Any, *, piper_frames: bytes, spoken: list[str]):
    cfg = load_config()
    cfg.backend = "local"
    cfg.wake_word = "ultron"
    cfg.wake_aliases = ""
    cfg.listen_early_check_ms = 800
    cfg.listen_after_speech_ms = 300
    cfg.listen_followup_s = 6
    cfg.voice_silence_ms = 2000
    cfg.recorder_warmup_ms = 300
    cfg.voice_min_snr_db = 10

    def speak(text: str) -> None:
        spoken.append(text)
        source.during_speech.extend(frames_of(piper_frames))  # mic hears Piper

    return HandsFreeListener(
        cfg,
        source,
        transcribe=transcriber,
        speak=speak,
        command_runner=runner,
        print_fn=lambda _line: None,
    )


def _runner(commands: list[str]):
    def run(command: str) -> tuple[str, bool]:
        commands.append(command)
        return "Done.", False

    return run


def _case(delay_ms: int, *, piper_frames: bytes | None = None, extra: bytes = b""):
    clip = read_clip()
    wake = silence(32) + tone(20) + silence(40)  # early check 800 ms after the voice
    delay = silence(delay_ms // 20)
    source = ScriptedSource(wake + delay + clip + silence(150) + extra)
    transcriber = ScriptedTranscriber([Transcript(True, "Ultron"), Transcript(True, "list files in my download folder")])
    commands: list[str] = []
    spoken: list[str] = []
    listener = _listener(
        source,
        transcriber,
        _runner(commands),
        piper_frames=piper_frames if piper_frames is not None else tone(50),
        spoken=spoken,
    )
    listener.run()
    return clip, transcriber, commands, spoken, listener


@pytest.mark.parametrize("delay_ms", [200, 500, 1500, 5000])
def test_command_after_yes_runs_with_the_whole_clip(delay_ms: int) -> None:
    clip, transcriber, commands, spoken, _listener = _case(delay_ms)

    assert spoken[0] == "Yes?"
    assert commands == ["list files in my download folder"]
    # The command audio reached Whisper with its beginning intact.
    command_pcm = transcriber.pcms[-1]
    assert clip[: len(clip) - FRAME_BYTES * 20] in command_pcm


def test_command_after_6500ms_is_ignored() -> None:
    _clip, _transcriber, commands, spoken, listener = _case(6500)

    assert spoken[0] == "Yes?"
    assert commands == []
    assert listener.ignored == 1


def test_loud_burst_while_piper_talks_does_not_raise_the_thresholds() -> None:
    _clip, _transcriber, commands, _spoken, listener = _case(500, piper_frames=tone(50, amp=32767))

    assert commands == ["list files in my download folder"]
    assert listener.detector.noise_floor < 1000


def test_two_commands_after_one_wake_word() -> None:
    clip = read_clip()
    wake = silence(32) + tone(20) + silence(40)
    source = ScriptedSource(wake + silence(25) + clip + silence(60) + silence(25) + clip + silence(150))
    transcriber = ScriptedTranscriber(
        [
            Transcript(True, "Ultron"),
            Transcript(True, "list files in my download folder"),
            Transcript(True, "list files in my download folder"),
        ]
    )
    commands: list[str] = []
    spoken: list[str] = []
    listener = _listener(source, transcriber, _runner(commands), piper_frames=tone(50), spoken=spoken)

    listener.run()

    assert commands == ["list files in my download folder", "list files in my download folder"]


def test_ultron_said_twice_keeps_the_window_open() -> None:
    clip = read_clip()
    wake = silence(32) + tone(20) + silence(40)
    second_wake = silence(32) + tone(20) + silence(40)
    source = ScriptedSource(wake + silence(25) + second_wake + silence(25) + clip + silence(150))
    transcriber = ScriptedTranscriber(
        [Transcript(True, "Ultron"), Transcript(True, "Ultron"), Transcript(True, "list files in my download folder")]
    )
    commands: list[str] = []
    spoken: list[str] = []
    listener = _listener(source, transcriber, _runner(commands), piper_frames=tone(50), spoken=spoken)

    listener.run()

    assert spoken[:2] == ["Yes?", "Yes?"]
    assert commands == ["list files in my download folder"]
