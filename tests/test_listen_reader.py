"""The mic is read continuously, so a busy assistant never loses or delays the next command."""

from __future__ import annotations

import array
import queue
import threading
import time
from collections import deque

from assistant.config import load_config
from assistant.interfaces.listen import ContinuousReader, HandsFreeListener, Transcript
from assistant.speech.recorder import FRAME_BYTES, FRAME_SAMPLES

FRAME = FRAME_SAMPLES


def tone(frames: int, amp: int = 3000) -> bytes:
    return array.array("h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]).tobytes()


def silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def frames_of(pcm: bytes) -> list[bytes]:
    return [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]


class LiveSource:
    """A mic-like source: one frame every 20 ms, silence when nothing is pushed."""

    def __init__(self) -> None:
        self.segments: deque[bytes] = deque()
        self.q: queue.Queue[bytes | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)

    def push(self, pcm: bytes) -> None:
        self.segments.extend(frames_of(pcm))

    def start(self) -> None:
        self._thread.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            frame = self.segments.popleft() if self.segments else silence(1)
            self.q.put(frame)
            time.sleep(0.02)
        self.q.put(None)

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return b""

    def stop(self) -> None:
        self._stop.set()


def test_reader_buffers_while_nobody_consumes_and_clear_drops_the_backlog() -> None:
    source = LiveSource()
    reader = ContinuousReader(source)
    reader.start()
    try:
        source.push(tone(10))
        deadline = time.monotonic() + 2.0
        while reader.buffered < 10 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert reader.buffered >= 10
        reader.clear()
        assert reader.buffered == 0
        source.push(tone(3))
        deadline = time.monotonic() + 2.0
        first = None
        while first is None and time.monotonic() < deadline:
            first = reader.get(0.1)
        assert first is not None and len(first) == FRAME_BYTES
    finally:
        reader.stop()
    assert reader.peak_frames >= 10


def test_listener_with_reader_handles_a_command_after_piper() -> None:
    cfg = load_config()
    cfg.backend = "local"
    cfg.wake_word = "ultron"
    cfg.listen_after_speech_ms = 300
    cfg.listen_followup_s = 6
    cfg.listen_early_check_ms = 800
    cfg.recorder_warmup_ms = 300
    cfg.voice_min_snr_db = 10

    source = LiveSource()
    reader = ContinuousReader(source)
    spoke_done = threading.Event()
    transcriptions = [Transcript(True, "Ultron"), Transcript(True, "list files in my download folder")]
    commands: list[str] = []
    spoken: list[str] = []

    def transcribe(_pcm: bytes) -> Transcript:
        return transcriptions.pop(0) if transcriptions else Transcript(True, "")

    def speak(_text: str) -> None:
        spoken.append(_text)
        source.push(tone(25))  # Piper audio, captured while it talks
        time.sleep(0.5)
        spoke_done.set()

    def runner(command: str) -> tuple[str, bool]:
        commands.append(command)
        return "Done.", False

    listener = HandsFreeListener(
        cfg, source, transcribe=transcribe, speak=speak, command_runner=runner,
        reader=reader, print_fn=lambda _line: None,
    )

    def controller() -> None:
        source.push(silence(32) + tone(20) + silence(40))  # "Ultron"
        assert spoke_done.wait(5.0)
        time.sleep(0.4)  # command starts after the 300 ms discard window
        source.push(silence(32) + tone(20) + silence(80))
        time.sleep(3.0)
        source.stop()

    threading.Thread(target=controller, daemon=True).start()
    listener.run()

    assert spoken[0] == "Yes?"
    assert commands == ["list files in my download folder"]
