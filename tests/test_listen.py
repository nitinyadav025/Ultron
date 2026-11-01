"""Hands-free mode: 12 offline cases with a scripted mic and a fake transcriber.

No microphone, no Piper playback, no LLM and no tool is opened. The source models "audio
that arrives while Piper talks" separately, so the drain/ignore window is tested exactly.
"""

from __future__ import annotations

import array
import random
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from assistant.config import load_config
from assistant.interfaces.listen import CONFIRMATION_REPLY, RETRY_REPLY, START_LINE, Transcript, HandsFreeListener
from assistant.safety.permissions import PermissionGate
from assistant.speech.recorder import FRAME_BYTES, FRAME_SAMPLES
from assistant.tools.registry import ToolRegistry, tool

FRAME = FRAME_SAMPLES


def tone(frames: int, amp: int = 3000) -> bytes:
    return array.array("h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]).tobytes()


def silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def utterance() -> bytes:
    """640 ms lead-in (past the 300 ms warm-up), 400 ms of voice, 2.4 s of silence."""
    return silence(32) + tone(20) + silence(150)


def noise(frames: int, amp: int = 4700, seed: int = 13) -> bytes:
    rng = random.Random(seed)
    return array.array("h", [rng.randint(-amp, amp) for _ in range(frames * FRAME)]).tobytes()


def frames_of(pcm: bytes) -> list[bytes]:
    return [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]


class ScriptedSource:
    """Queued audio, plus frames that "arrive while Piper speaks" (read with timeout 0)."""

    def __init__(self, pcm: bytes = b"", *, keyboard_interrupt: bool = False) -> None:
        self.queue: deque[bytes] = deque(frames_of(pcm))
        self.during_speech: deque[bytes] = deque()
        self.started = False
        self.stopped = False
        self.keyboard_interrupt = keyboard_interrupt

    def start(self) -> None:
        if self.keyboard_interrupt:
            raise KeyboardInterrupt
        self.started = True

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        if self.keyboard_interrupt:
            raise KeyboardInterrupt
        if timeout == 0:
            return self.during_speech.popleft() if self.during_speech else b""
        if self.queue:
            return self.queue.popleft()
        return None

    def stop(self) -> None:
        self.stopped = True


class ScriptedTranscriber:
    def __init__(self, responses: list[Transcript]) -> None:
        self.responses = list(responses)
        self.calls: list[int] = []

    def __call__(self, pcm: bytes) -> Transcript:
        self.calls.append(len(pcm))
        if not self.responses:
            return Transcript(True, "")
        return self.responses.pop(0)


def _listener(
    source: ScriptedSource,
    transcriber: Any,
    runner: Any,
    *,
    speak: Any = None,
    save_debug: Any = None,
    prints: list[str] | None = None,
) -> HandsFreeListener:
    cfg = load_config()
    cfg.backend = "local"
    cfg.wake_word = "ultron"
    cfg.wake_aliases = ""
    cfg.listen_early_check_ms = 800
    cfg.listen_after_speech_ms = 600
    cfg.listen_followup_s = 6
    cfg.voice_silence_ms = 2000
    cfg.voice_max_seconds = 30
    cfg.recorder_warmup_ms = 300
    cfg.voice_min_snr_db = 10
    out = prints if prints is not None else []
    return HandsFreeListener(
        cfg,
        source,
        transcribe=transcriber,
        speak=speak or (lambda _text: None),
        command_runner=runner,
        save_debug=save_debug,
        print_fn=out.append,
    )


def _runner(log: list[str], reply: str = "Done.", refused: bool = False):
    def run(command: str) -> tuple[str, bool]:
        log.append(command)
        return reply, refused

    return run


# 1) 60 s of noise: no print, no tool, no speech ---------------------------------------
def test_sixty_seconds_of_noise_is_completely_ignored() -> None:
    source = ScriptedSource(noise(3000))
    transcriber = ScriptedTranscriber([])
    commands: list[str] = []
    spoken: list[str] = []
    prints: list[str] = []
    listener = _listener(source, transcriber, _runner(commands), speak=spoken.append, prints=prints)

    assert listener.run() == 0

    assert prints == [START_LINE]
    assert transcriber.calls == []  # nothing reached Whisper
    assert commands == [] and spoken == []
    assert listener.ignored == 0
    assert source.stopped is True


# 2) speech without the wake word: silent, nothing saved or logged ---------------------
def test_speech_without_the_wake_word_is_ignored_silently() -> None:
    source = ScriptedSource(utterance())
    transcriber = ScriptedTranscriber([Transcript(True, "open firefox")])
    commands: list[str] = []
    saved: list[Any] = []
    prints: list[str] = []
    listener = _listener(source, transcriber, _runner(commands), save_debug=lambda *a: saved.append(a), prints=prints)

    listener.run()

    assert commands == [] and saved == []
    assert listener.ignored == 1
    # One short screen line, with no words from the speech.
    assert prints == [START_LINE, "\u00b7 heard speech, no wake word"]


# 3) "ultron open firefox": one tool call, wake word removed ---------------------------
def test_wake_word_command_runs_once_without_the_wake_word() -> None:
    source = ScriptedSource(utterance())
    transcriber = ScriptedTranscriber([Transcript(True, "Ultron, open Firefox.", raw="Ultron, open Firefox.")])
    commands: list[str] = []
    listener = _listener(source, transcriber, _runner(commands))

    listener.run()

    assert commands == ["open firefox"]
    assert listener.commands_run == 1


# 4) wake word only: "Yes?", next sentence works, then the window closes ---------------
def test_wake_word_only_opens_a_six_second_window() -> None:
    pcm = utterance() + utterance()
    transcriber = ScriptedTranscriber([Transcript(True, "Ultron"), Transcript(True, "open firefox")])
    commands: list[str] = []
    spoken: list[str] = []
    listener = _listener(ScriptedSource(pcm), transcriber, _runner(commands), speak=spoken.append)

    listener.run()

    assert spoken[0] == "Yes?"
    assert commands == ["open firefox"]


def test_after_the_window_the_wake_word_is_required_again() -> None:
    pcm = utterance() + silence(500) + utterance()
    transcriber = ScriptedTranscriber([Transcript(True, "Ultron"), Transcript(True, "open firefox")])
    commands: list[str] = []
    spoken: list[str] = []
    listener = _listener(ScriptedSource(pcm), transcriber, _runner(commands), speak=spoken.append)

    listener.run()

    assert spoken[0] == "Yes?"
    assert commands == []  # outside the 6 s window, no wake word: ignored
    assert listener.ignored == 1


# 5) a 1.5 s pause inside a sentence stays one command --------------------------------
def test_pause_inside_a_sentence_keeps_one_command() -> None:
    pcm = silence(32) + tone(20) + silence(75) + tone(20) + silence(120)
    transcriber = ScriptedTranscriber(
        [Transcript(True, "Ultron open firefox and"), Transcript(True, "Ultron open firefox and list my downloads")]
    )
    commands: list[str] = []
    listener = _listener(ScriptedSource(pcm), transcriber, _runner(commands))

    listener.run()

    assert commands == ["open firefox and list my downloads"]
    assert len(transcriber.calls) == 2


# 6) dangling "and": wait, then finish with the rest ----------------------------------
def test_dangling_word_waits_and_then_finishes() -> None:
    pcm = utterance()
    transcriber = ScriptedTranscriber(
        [Transcript(True, "ultron open firefox and"), Transcript(True, "ultron open firefox and list my downloads")]
    )
    commands: list[str] = []
    listener = _listener(ScriptedSource(pcm), transcriber, _runner(commands))

    listener.run()

    assert commands == ["open firefox and list my downloads"]


# 7) audio during Piper speech and the 600 ms after it is ignored ----------------------
def test_audio_during_speech_and_six_hundred_ms_after_is_ignored() -> None:
    source = ScriptedSource(utterance() + utterance())
    transcriber = ScriptedTranscriber(
        [Transcript(True, "ultron mute"), Transcript(True, "ultron open firefox")]
    )
    commands: list[str] = []
    spoken: list[str] = []

    def speak(text: str) -> None:
        spoken.append(text)
        if len(spoken) == 1:  # what the mic hears while Piper is talking
            source.during_speech.extend(frames_of(tone(20) + silence(4)))
            # and a whole command in the 600 ms after it stops
            source.queue.extendleft(reversed(frames_of(tone(20) + silence(8))))

    listener = _listener(source, transcriber, _runner(commands), speak=speak)

    listener.run()

    assert spoken[0] == "Done."
    # the command heard during/after speech was never transcribed or run
    assert commands == ["mute", "open firefox"]
    assert len(transcriber.calls) == 2


# 8) two commands back to back ---------------------------------------------------------
def test_two_commands_back_to_back() -> None:
    pcm = utterance() + utterance()
    transcriber = ScriptedTranscriber([Transcript(True, "ultron open firefox"), Transcript(True, "ultron mute")])
    commands: list[str] = []
    listener = _listener(ScriptedSource(pcm), transcriber, _runner(commands))

    listener.run()

    assert commands == ["open firefox", "mute"]


# 9) unreliable transcript with the wake word -----------------------------------------
def test_unreliable_transcript_after_the_wake_word_asks_again_and_runs_nothing() -> None:
    source = ScriptedSource(utterance())
    transcriber = ScriptedTranscriber(
        [Transcript(False, "I didn't catch that, please say it again.", raw="Ultron open firefox")]
    )
    commands: list[str] = []
    spoken: list[str] = []
    listener = _listener(source, transcriber, _runner(commands), speak=spoken.append)

    listener.run()

    assert spoken[0] == RETRY_REPLY
    assert commands == []


# 10) a confirmation-needed tool is refused by voice and not run -----------------------
def test_confirmation_needed_tool_is_refused_by_voice() -> None:
    source = ScriptedSource(utterance())
    transcriber = ScriptedTranscriber([Transcript(True, "Ultron delete my notes")])
    commands: list[str] = []
    spoken: list[str] = []
    listener = _listener(
        source, transcriber, _runner(commands, reply="ignored", refused=True), speak=spoken.append
    )

    listener.run()

    assert commands == ["delete my notes"]
    assert spoken[0] == CONFIRMATION_REPLY


def test_permission_gate_never_prompts_in_hands_free_mode_and_runs_nothing() -> None:
    executed: list[str] = []

    @tool(risk_level="confirm", read_only=False)
    def delete_thing(path: str) -> dict[str, Any]:
        """Delete something."""
        executed.append(path)
        return {"ok": True}

    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="ask", confirm_callback=lambda *_a: pytest.fail("prompted"))
    registry = ToolRegistry(config=cfg, gate=gate, allow_prompts=False)
    registry.register(delete_thing)

    result = registry.execute("delete_thing", {"path": "notes.txt"})

    assert result["ok"] is False
    assert result["needs_confirmation"] is True
    assert registry.needs_confirmation_seen is True
    assert executed == []


# 11) Ctrl+C stops cleanly and calls source.stop() ------------------------------------
def test_ctrl_c_stops_cleanly_and_calls_source_stop() -> None:
    source = ScriptedSource(tone(20), keyboard_interrupt=True)
    listener = _listener(source, ScriptedTranscriber([]), _runner([]))

    assert listener.run() == 0
    assert source.stopped is True


# 12) a loud burst in the first 300 ms is ignored -------------------------------------
def test_a_loud_burst_in_the_first_300_ms_is_ignored() -> None:
    burst = tone(15, amp=32767)  # exactly the 300 ms warm-up
    source = ScriptedSource(burst + silence(12) + tone(20) + silence(120))
    transcriber = ScriptedTranscriber([Transcript(True, "ultron open firefox")])
    commands: list[str] = []
    listener = _listener(source, transcriber, _runner(commands))

    listener.run()

    assert commands == ["open firefox"]


# Debug capture is only written for utterances that passed the wake word --------------
def test_debug_audio_is_saved_only_after_the_wake_word(tmp_path: Path) -> None:
    saved: list[tuple[bytes, Transcript, str]] = []
    pcm = utterance() + utterance()
    transcriber = ScriptedTranscriber([Transcript(True, "open firefox"), Transcript(True, "ultron mute")])
    listener = _listener(
        ScriptedSource(pcm),
        transcriber,
        _runner([]),
        save_debug=lambda data, transcript, command: saved.append((data, transcript, command)),
    )

    listener.run()

    assert [item[2] for item in saved] == ["mute"]


def test_ignored_speech_never_puts_its_words_in_the_log_or_on_screen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="ultron.listen")
    prints: list[str] = []
    listener = _listener(
        ScriptedSource(utterance()),
        ScriptedTranscriber([Transcript(True, "open firefox")]),
        _runner([]),
        prints=prints,
    )

    listener.run()

    assert "open firefox" not in caplog.text
    assert all("open firefox" not in line for line in prints)
    assert "result=no_wake_word" in caplog.text
    assert "duration=" in caplog.text and "snr=" in caplog.text


def test_listen_log_ignored_prints_only_the_first_word(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="ultron.listen")
    source = ScriptedSource(utterance())
    listener = _listener(
        source,
        ScriptedTranscriber([Transcript(True, "open firefox")]),
        _runner([]),
    )
    listener.config.listen_log_ignored = True

    listener.run()

    assert "ignored speech starting with 'open'" in caplog.text
    assert "open firefox" not in caplog.text


def test_debug_text_is_screen_only(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="ultron.listen")
    prints: list[str] = []
    listener = _listener(
        ScriptedSource(utterance()),
        ScriptedTranscriber([Transcript(True, "open firefox")]),
        _runner([]),
        prints=prints,
    )
    listener.config.listen_debug = True

    listener.run()

    assert any('debug: heard "open firefox"' in line for line in prints)
    assert any("debug: wake score=" in line for line in prints)
    assert "open firefox" not in caplog.text


def test_listen_uses_the_same_level_repair_as_voice() -> None:
    import numpy as np

    from assistant.interfaces.listen import pcm_to_float32

    quiet = tone(20, amp=300)
    raw = np.frombuffer(quiet, dtype="<i2").astype(np.float32) / 32768.0
    repaired = pcm_to_float32(quiet)
    assert repaired.dtype == np.float32
    assert float(np.sqrt(np.mean(repaired**2))) > 2.0 * float(np.sqrt(np.mean(raw**2)))
