"""Hands-free mode (`main.py listen`): one mic stream, a wake word, early endpointing.

The microphone stream is opened **once** and never restarted. A ring buffer keeps the
400 ms before speech, the existing recorder thresholds (hysteresis, adaptive noise floor,
10 dB SNR) decide what is voice, and the sentence is transcribed after
`LISTEN_EARLY_CHECK_MS` of silence. A sentence that stops on "and", "the", a comma, etc.
keeps listening until `VOICE_SILENCE_MS` and is transcribed again.

Everything that decides, speaks or runs a command is injected, so the tests replay
scripted audio with a fake transcriber and a fake command runner: no microphone, no
Piper playback, no LLM, no tool.
"""

from __future__ import annotations

import array
import json
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from assistant.config import AssistantConfig
from assistant.speech.ears import NO_SPEECH
from assistant.speech.recorder import (
    CALIBRATION_MS,
    DEFAULT_NOISE_FLOOR,
    DEFAULT_WARMUP_MS,
    FRAME_BYTES,
    FRAME_MS,
    MIN_NOISE_FLOOR,
    PRE_ROLL_MS,
    AdaptiveWarmup,
    PwRecordSource,
    _end_threshold,
    _is_clipped_burst,
    _start_threshold,
    frame_peak,
    frame_rms,
    pcm16_samples,
    percentile,
    samples_to_pcm16,
    write_pcm16_wav,
)
from assistant.speech.wake import aliases_for, first_word, sentence_is_complete, wake_match
from assistant.tools.registry import ToolRegistry

logger = logging.getLogger("ultron.listen")

START_LINE = "Listening. Say 'Ultron, <command>'. Ctrl+C to stop."
CONFIRMATION_REPLY = "That needs confirmation. Please type it in the chat."
RETRY_REPLY = "Sorry, say that again."
YES_REPLY = "Yes?"
MIN_SPEECH_MS = 250
TRAILING_SILENCE_MS = 300
STEADY_SPEECH_LIMIT_MS = 8000
STEADY_LEVEL_CV = 0.06
STOP_COMMANDS = {"stop listening", "stop"}


@dataclass(frozen=True)
class Transcript:
    """One transcription result; ``raw`` is what Whisper wrote even when the guard rejects."""

    ok: bool
    text: str
    raw: str = ""
    reason: str = ""


class UtteranceDetector:
    """State machine over 20 ms frames: waiting -> speech -> silence -> done."""

    def __init__(
        self,
        *,
        warmup_ms: int = DEFAULT_WARMUP_MS,
        warmup_max_ms: int = 1500,
        min_snr_db: float = 10.0,
        silence_ms: int = 2000,
        early_ms: int = 800,
        max_seconds: float = 30.0,
    ) -> None:
        self.warmup_ms = int(warmup_ms)
        self._warm = AdaptiveWarmup(self.warmup_ms, warmup_max_ms)
        self._warmup_end_ms = self.warmup_ms
        self.min_snr_db = float(min_snr_db)
        self.silence_ms = int(silence_ms)
        self.early_ms = int(early_ms)
        self.max_seconds = float(max_seconds)

        self.audio_ms = 0
        self.last_start_ms = 0
        self.last_reason = ""
        self.noise_floor = DEFAULT_NOISE_FLOOR
        self._calibrated = False
        self._calibration: list[int] = []
        self._ring: deque[array.array] = deque(maxlen=max(1, (PRE_ROLL_MS + MIN_SPEECH_MS) // FRAME_MS))
        self._collected: list[array.array] = []
        self._in_speech = False
        self._speech_activity = 0.0
        self._silence_run = 0
        self._speech_frames = 0
        self._start_ms = 0
        self._speaker_level = 0.0
        self._early_sent = False
        self._steady_checked = False

    # -- helpers -----------------------------------------------------------------

    def _thresholds(self) -> tuple[float, float]:
        start = _start_threshold(self.noise_floor, self.min_snr_db)
        end = _end_threshold(self.noise_floor, start, self._speaker_level)
        return start, end

    def skip(self, frame: array.array) -> None:
        """Advance audio time for discarded audio (during/after Piper) without detecting."""
        self.audio_ms += FRAME_MS

    def reset(self) -> None:
        self._ring.clear()
        self._collected = []
        self._in_speech = False
        self._speech_activity = 0.0
        self._silence_run = 0
        self._speech_frames = 0
        self._start_ms = 0
        self._speaker_level = 0.0
        self._early_sent = False
        self._steady_checked = False
        self.last_reason = ""

    # -- main step ---------------------------------------------------------------

    def feed(self, frame: array.array) -> str | None:
        """Consume one 20 ms frame. Return "early", "final", "noise" or None."""
        self.audio_ms += FRAME_MS

        level = frame_rms(frame)
        peak = frame_peak(frame)
        burst = _is_clipped_burst(level, peak)

        if not self._warm.done:
            if self._warm.feed(level, peak, self.audio_ms):
                return None
            self._warmup_end_ms = self._warm.end_ms
            self._calibrated = False
            self._calibration.clear()
            return None  # the frame that ended the warm-up is discarded too

        if not self._calibrated:
            if not burst:
                self._calibration.append(level)
            if self.audio_ms >= self._warmup_end_ms + CALIBRATION_MS:
                estimate = float(percentile(self._calibration, 0.25)) if self._calibration else DEFAULT_NOISE_FLOOR
                self.noise_floor = max(MIN_NOISE_FLOOR, estimate)
                self._calibrated = True

        start_thr, end_thr = self._thresholds()
        is_speech = level >= start_thr and not burst

        if not self._in_speech:
            self._ring.append(frame)
            if is_speech:
                self._speech_activity = min(1.0, self._speech_activity + FRAME_MS / MIN_SPEECH_MS)
                self._speech_frames += 1
            else:
                self._speech_activity = max(0.0, self._speech_activity - FRAME_MS / (2 * MIN_SPEECH_MS))
                if self._calibrated and level <= self.noise_floor * 1.5:
                    self.noise_floor = max(MIN_NOISE_FLOOR, 0.95 * self.noise_floor + 0.05 * level)
            if self._speech_activity >= 1.0:
                self._in_speech = True
                self._start_ms = max(0, self.audio_ms - MIN_SPEECH_MS)
                self.last_start_ms = self._start_ms
                self._speaker_level = float(max(frame_rms(item) for item in self._ring))
                self._collected = list(self._ring)
                self._ring.clear()
            return None

        self._collected.append(frame)
        speech_like = level >= start_thr and not burst
        if speech_like:
            self._silence_run = 0
            self._speaker_level = max(self._speaker_level, float(level))
            self._speech_frames += 1
        elif level < end_thr:
            self._silence_run += 1
        else:
            self._silence_run = 0

        elapsed = self.audio_ms - self._start_ms
        if not self._early_sent and self._silence_run * FRAME_MS >= self.early_ms:
            self._early_sent = True
            return "early"
        if self._silence_run * FRAME_MS >= self.silence_ms and elapsed >= 1500:
            self.last_reason = "silence"
            return "final"
        if elapsed >= int(self.max_seconds * 1000):
            self.last_reason = "max_length"
            return "final"
        if not self._steady_checked and elapsed >= STEADY_SPEECH_LIMIT_MS:
            self._steady_checked = True
            levels = [frame_rms(item) for item in self._collected]
            mean = sum(levels) / len(levels)
            if mean > 0:
                variance = sum((item - mean) ** 2 for item in levels) / len(levels)
                if variance**0.5 / mean < STEADY_LEVEL_CV:
                    self.last_reason = "constant_noise"
                    self.reset()
                    return "noise"
        return None

    def take_pcm(self, *, trim_tail: bool = True) -> bytes:
        frames = list(self._collected)
        if trim_tail and frames:
            _, end_thr = self._thresholds()
            last = 0
            for index, frame in enumerate(frames):
                if frame_rms(frame) >= end_thr:
                    last = index
            frames = frames[: last + 1 + TRAILING_SILENCE_MS // FRAME_MS]
        return b"".join(samples_to_pcm16(frame) for frame in frames)

    def feed_warm(self, frames: list[array.array]) -> None:
        """Re-process the tail of a discard window for start detection only.

        The noise floor and calibration are never touched here, so a burst during Piper
        cannot raise the thresholds. This is what keeps the first word of a command that
        starts during the discard window.
        """
        if not frames:
            return
        if self._in_speech:
            self._collected.extend(frames)
            return
        start_thr, _end = self._thresholds()
        for frame in frames:
            level = frame_rms(frame)
            burst = _is_clipped_burst(level, frame_peak(frame))
            self._ring.append(frame)
            if level >= start_thr and not burst:
                self._speech_activity = min(1.0, self._speech_activity + FRAME_MS / MIN_SPEECH_MS)
                self._speech_frames += 1
            else:
                self._speech_activity = max(0.0, self._speech_activity - FRAME_MS / (2 * MIN_SPEECH_MS))
            if self._speech_activity >= 1.0:
                self._in_speech = True
                self._start_ms = max(0, self.audio_ms - MIN_SPEECH_MS)
                self.last_start_ms = self._start_ms
                self._speaker_level = float(max(frame_rms(item) for item in self._ring))
                self._collected = list(self._ring)
                self._ring.clear()
                break

    def stats(self) -> dict[str, Any]:
        """Peak/RMS/floor/SNR for the utterance log line (no text involved)."""
        frames = list(self._collected)
        levels = [frame_rms(frame) for frame in frames] or [0]
        speech_level = percentile(levels, 0.75) if frames else 0
        snr_db = 0.0
        if speech_level > 0 and self.noise_floor > 0:
            snr_db = 20.0 * math.log10(float(speech_level) / float(self.noise_floor))
        return {
            "duration_ms": max(0, self.audio_ms - self._start_ms),
            "peak": max((frame_peak(frame) for frame in frames), default=0),
            "rms": int(sum(levels) / len(levels)),
            "noise_floor": self.noise_floor,
            "snr_db": snr_db,
        }

    @property
    def ready(self) -> bool:
        """True once the opening pop has been discarded."""
        return self._warm.done

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def speech_ms(self) -> int:
        return self._speech_frames * FRAME_MS


class ContinuousReader:
    """Read the mic continuously in a thread into a bounded ring with timestamps.

    The pipe is therefore never left unread while Whisper, the LLM or Piper is busy; the
    newest 40 s are kept and the oldest frames are dropped. ``clear()`` removes the
    backlog that arrived while the assistant was talking, so the next utterance starts
    fresh with no stale audio in front of it.
    """

    def __init__(self, source: Any, max_frames: int = 2000) -> None:
        self.source = source
        self._frames: deque[tuple[float, bytes]] = deque(maxlen=max_frames)
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._ended = False
        self._thread: threading.Thread | None = None
        self.peak_frames = 0

    def start(self) -> None:
        self.source.start()
        self._thread = threading.Thread(target=self._pump, name="mic-reader", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            chunk = self.source.read(FRAME_BYTES, timeout=0.25)
            if chunk is None:
                with self._cv:
                    self._ended = True
                    self._cv.notify_all()
                return
            if not chunk:
                continue
            with self._cv:
                self._frames.append((time.monotonic(), chunk))
                self.peak_frames = max(self.peak_frames, len(self._frames))
                self._cv.notify_all()

    def get(self, timeout: float) -> bytes | None:
        stamped = self.get_stamped(timeout)
        return None if stamped is None else stamped[1]

    def get_stamped(self, timeout: float) -> tuple[float, bytes] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while not self._frames:
                if self._ended:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cv.wait(remaining)
            return self._frames.popleft()

    def clear(self) -> None:
        with self._cv:
            self._frames.clear()

    @property
    def buffered(self) -> int:
        with self._cv:
            return len(self._frames)

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        try:
            self.source.stop()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=0.5)


class HandsFreeListener:
    """Reads one long-lived source and turns utterances into commands."""

    def __init__(
        self,
        config: AssistantConfig,
        source: Any,
        *,
        transcribe: Callable[[bytes], Transcript],
        speak: Callable[[str], None],
        command_runner: Callable[[str], tuple[str, bool]],
        reader: ContinuousReader | None = None,
        save_debug: Callable[[bytes, Transcript, str], None] | None = None,
        print_fn: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.source = source
        self.transcribe = transcribe
        self.speak = speak
        self.command_runner = command_runner
        self.reader = reader
        self.save_debug = save_debug
        self.print_fn = print_fn
        self.clock = clock
        self.detector = UtteranceDetector(
            warmup_ms=getattr(config, "recorder_warmup_ms", DEFAULT_WARMUP_MS),
            warmup_max_ms=getattr(config, "recorder_warmup_max_ms", 1500),
            min_snr_db=getattr(config, "voice_min_snr_db", 5.0),
            silence_ms=getattr(config, "voice_silence_ms", 2000),
            early_ms=getattr(config, "listen_early_check_ms", 800),
            max_seconds=getattr(config, "voice_max_seconds", 30.0),
        )
        self.ignored = 0
        self.commands_run = 0
        self.stop_requested = False
        self.followup_until_ms = 0
        self.ignore_until_ms = 0
        self._carry = b""
        self._ready = False
        self._discard_tail: deque[array.array] = deque(maxlen=max(1, 400 // FRAME_MS))
        self._discard_active = False
        self._followup_active = False
        self._t_end = 0.0
        self._t_endpoint = 0.0
        self._t_trans = 0.0
        self._first_tool: float | None = None

    def note_tool_call(self, _name: str = "") -> None:
        """Called by the tool registry on the first tool execution of a command."""
        if self._first_tool is None:
            self._first_tool = self.clock()

    # -- public -------------------------------------------------------------------

    def run(self) -> int:
        try:
            if self.reader is not None:
                self.reader.start()
            else:
                self.source.start()
            while not self.stop_requested:
                if self.reader is not None:
                    chunk = self.reader.get(0.25)
                else:
                    chunk = self.source.read(FRAME_BYTES, timeout=0.25)
                if chunk is None:
                    break
                if not chunk:
                    continue
                self._consume(chunk)
        except KeyboardInterrupt:
            logger.info("Hands-free listening stopped by Ctrl+C.")
        finally:
            if self.reader is not None:
                self.reader.stop()
            else:
                self.source.stop()
        return 0

    # -- internals ----------------------------------------------------------------

    def _say(self, line: str) -> None:
        self.print_fn(f"\u00b7 {line}")

    def _debug(self, line: str) -> None:
        """LISTEN_DEBUG=1: screen only, never a log line or a file."""
        if getattr(self.config, "listen_debug", False):
            self.print_fn(f"\u00b7 debug: {line}")

    def _consume(self, chunk: bytes) -> None:
        self._carry += chunk
        while len(self._carry) >= FRAME_BYTES and not self.stop_requested:
            raw_frame, self._carry = self._carry[:FRAME_BYTES], self._carry[FRAME_BYTES:]
            frame = pcm16_samples(raw_frame)

            if not self._ready and self.detector.ready:
                self._ready = True
                self.print_fn(START_LINE)

            if self.detector.audio_ms < self.ignore_until_ms:
                self.detector.skip(frame)
                self._discard_tail.append(frame)
                continue
            if self._discard_active:
                self._discard_active = False
                # The last 400 ms are kept as pre-roll and checked for a speech start,
                # without letting a burst change the noise floor.
                self.detector.feed_warm(list(self._discard_tail))
                self._discard_tail.clear()
                logger.info("Hands-free: discard window closed.")
            if (
                self._followup_active
                and self.detector.audio_ms > self.followup_until_ms
                and not self.detector.in_speech
            ):
                # Never close the window in the middle of the user's sentence: a command
                # that started inside the window must be accepted even if it ends later.
                self._followup_active = False
                logger.info("Hands-free: follow-up window closed.")
                self._say("window closed")

            event = self.detector.feed(frame)
            if event == "early":
                self._handle_early()
            elif event == "final":
                self._handle_final()
            elif event == "noise":
                logger.info("Hands-free: steady noise ignored.")

    def _transcribe(self, pcm: bytes) -> Transcript:
        return self.transcribe(pcm)

    def _log_utterance(self, result: str) -> None:
        """One INFO line per detected utterance, with no words from the speech."""
        stats = self.detector.stats()
        now = datetime.now()
        start = datetime.fromtimestamp(now.timestamp() - stats["duration_ms"] / 1000.0)
        logger.info(
            "Utterance: start=%s end=%s duration=%.2fs peak=%d rms=%d noise_floor=%d "
            "snr=%.1f dB reason=%s sent_to_whisper=yes result=%s",
            start.strftime("%H:%M:%S"),
            now.strftime("%H:%M:%S"),
            stats["duration_ms"] / 1000.0,
            stats["peak"],
            stats["rms"],
            int(stats["noise_floor"]),
            stats["snr_db"],
            self.detector.last_reason or "early",
            result,
        )

    def _handle_early(self) -> None:
        self._t_end = self.clock()
        pcm = self.detector.take_pcm(trim_tail=False)
        self._t_endpoint = self.clock()
        transcript = self._transcribe(pcm)
        self._t_trans = self.clock()
        self._debug(f'heard "{transcript.text}"')
        if not transcript.ok:
            # A rejected decode cannot be trusted either way; process it now so a wake word
            # that is still readable in the raw text gets "Sorry, say that again.".
            self._process(transcript, pcm)
        elif sentence_is_complete(transcript.text):
            self._process(transcript, pcm)
        else:
            logger.info("Hands-free: sentence not finished yet (dangling word).")

    def _handle_final(self) -> None:
        self._t_end = self.clock()
        pcm = self.detector.take_pcm()
        self._t_endpoint = self.clock()
        transcript = self._transcribe(pcm)
        self._t_trans = self.clock()
        self._debug(f'heard "{transcript.text}"')
        self._process(transcript, pcm)

    def _process(self, transcript: Transcript, pcm: bytes) -> None:
        started = self.detector.last_start_ms
        in_followup = self._followup_active and started <= self.followup_until_ms
        wake_text = transcript.raw or transcript.text
        wake_word = getattr(self.config, "wake_word", "ultron")
        wake_aliases = getattr(self.config, "wake_aliases", "")
        match = wake_match(wake_text, wake_word, wake_aliases)
        self._debug(
            f"wake score={match.ratio:.2f} target={match.target or '-'} "
            f"aliases={','.join(aliases_for(wake_word, wake_aliases))}"
        )
        wake_found = match.found
        command = match.command
        used_followup = False
        if not wake_found and in_followup:
            wake_found = True
            used_followup = True
            command = re.sub(r"\s+", " ", transcript.text or "").strip()

        if not wake_found:
            self.ignored += 1
            if getattr(self.config, "listen_log_ignored", False):
                logger.info("Hands-free: ignored speech starting with %r", first_word(wake_text))
            self._log_utterance("no_wake_word")
            self._say("heard speech, no wake word")
            self.detector.reset()
            return

        self._say("wake word heard")

        if not transcript.ok:
            self._log_utterance("too_short" if transcript.text == NO_SPEECH else "rejected_by_guard")
            self.detector.reset()
            self._speak(RETRY_REPLY)
            return

        if not command:
            self._log_utterance("wake_only")
            self.detector.reset()
            self._speak(YES_REPLY)
            return

        if command.strip(" .!?").lower() in STOP_COMMANDS:
            self._log_utterance("stop_command")
            self.detector.reset()
            self.stop_requested = True
            return

        self._log_utterance("follow_up" if used_followup else "accepted")
        self.detector.reset()
        if self.save_debug is not None:
            self.save_debug(pcm, transcript, command)

        self._first_tool = None
        reply, refused = self.command_runner(command)
        if refused:
            reply = CONFIRMATION_REPLY
        reply_ready = self.clock()
        speech_start = self.clock() - self._t_end
        self.commands_run += 1
        logger.info(
            "Latency: end_to_endpoint=%.2fs transcribe=%.2fs first_tool=%s reply_ready=%.2fs speech_start=%.2fs",
            self._t_endpoint - self._t_end,
            self._t_trans - self._t_endpoint,
            "n/a" if self._first_tool is None else f"{self._first_tool - self._t_trans:.2f}s",
            reply_ready - self._t_trans,
            speech_start,
        )
        self._speak(reply)

    def _speak(self, text: str) -> None:
        self.speak(text)
        self._drain()
        after = int(getattr(self.config, "listen_after_speech_ms", 600))
        window = int(getattr(self.config, "listen_followup_s", 6.0) * 1000)
        self.ignore_until_ms = self.detector.audio_ms + after
        self.followup_until_ms = self.ignore_until_ms + window
        self._discard_tail.clear()
        self._discard_active = True
        self._followup_active = True
        logger.info("Hands-free: discard window open for %d ms.", after)
        logger.info("Hands-free: follow-up window open for %d ms.", window)
        self._say(f"waiting for your command ({window // 1000} s)")

    def _drain(self) -> None:
        if self.reader is not None:
            # Drop only what arrived while the assistant was busy. A reader thread keeps
            # the pipe empty, so the next utterance is fresh and never delayed.
            self.reader.clear()
            return
        while True:
            chunk = self.source.read(FRAME_BYTES, timeout=0)
            if not chunk:
                return
            self._carry += chunk
            while len(self._carry) >= FRAME_BYTES:
                raw_frame, self._carry = self._carry[:FRAME_BYTES], self._carry[FRAME_BYTES:]
                self.detector.skip(pcm16_samples(raw_frame))


def save_debug_attempt(directory: Path, pcm: bytes, transcript: Transcript, command: str, keep: int = 10) -> Path:
    """Save WAV + JSON for an utterance that *passed* the wake word; newest `keep` stay."""
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stem = f"listen_{now:%Y%m%d-%H%M%S-%f}"
    write_pcm16_wav(directory / f"{stem}.wav", pcm, rate=16000, channels=1)
    payload = {
        "timestamp": now.isoformat(timespec="seconds"),
        "mode": "listen",
        "command": command,
        "transcript": transcript.text,
        "raw": transcript.raw,
        "reason": transcript.reason,
    }
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stems = sorted({item.stem for item in directory.glob("listen_*")}, reverse=True)
    for old in stems[keep:]:
        for suffix in (".wav", ".json"):
            (directory / f"{old}{suffix}").unlink(missing_ok=True)
    return path


def run_listen(config: AssistantConfig) -> int:
    """Build the real hands-free session (mic, ears, mouth, tools, agent loop) and run it."""
    import numpy as np

    from assistant.brain import build_adapter
    from assistant.brain.agent_loop import AgentLoop
    from assistant.brain.fast_path import try_fast_path
    from assistant.config import WORKSPACE_DIR
    from assistant.interfaces.voice import VoiceController
    from assistant.safety.killswitch import WatchdogMonitor, register_gnome_kill_hotkey
    from assistant.safety.permissions import PermissionGate
    from assistant.speech.mic_input import apply_mic_source_volume
    from assistant.tools.apps import open_app
    from assistant.tools.files import list_files, read_file
    from assistant.tools.volume import mute_audio, set_volume, volume_down, volume_up
    from assistant.tools.web import open_first_result, web_search

    register_gnome_kill_hotkey(config.kill_hotkey_binding)
    _mic_ok, mic_msg = apply_mic_source_volume(config.mic_source_volume)
    if config.mic_source_volume:
        print(f"  Mic input: {mic_msg}", flush=True)

    gate = PermissionGate(config=config, mode=config.default_mode)
    registry = ToolRegistry(config=config, gate=gate, allow_prompts=False)
    for fn in (list_files, read_file, open_app, volume_up, volume_down, set_volume, mute_audio, web_search, open_first_result):
        registry.register(fn)

    adapter = build_adapter(config)
    watchdog = WatchdogMonitor(config.max_steps, config.wall_clock_timeout_sec, config.idle_standdown_sec)
    loop = AgentLoop(config, adapter, registry, watchdog)
    voice = VoiceController(config=config, adapter=adapter)

    ok_ears, ears_msg = voice.load_ears()
    if not ok_ears:
        print(f"[ERROR] Ears unavailable: {ears_msg}", flush=True)
        return 1
    print(f"  Ears: ready \u2014 {ears_msg}", flush=True)
    ok_mouth, mouth_msg = voice.load_mouth()
    if not ok_mouth:
        print(f"[ERROR] Mouth unavailable: {mouth_msg}", flush=True)
        return 1
    print(f"  Mouth: ready \u2014 {mouth_msg}", flush=True)

    def transcribe(pcm: bytes) -> Transcript:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        ok, text = voice.ears.transcribe_samples(samples, log_text=False)
        confidence = dict(getattr(voice.ears, "last_confidence", {}) or {})
        return Transcript(
            ok=ok,
            text=text,
            raw=str(confidence.get("text") or (text if ok else "")),
            reason=str(confidence.get("verdict") or ""),
        )

    def speak(text: str) -> None:
        try:
            voice.speak_sync(text)
        except KeyboardInterrupt:
            voice.stop_speaking()

    def run_command(command: str) -> tuple[str, bool]:
        registry.needs_confirmation_seen = False
        fast_reply = try_fast_path(registry, command)
        if fast_reply is not None:
            return fast_reply, False
        result = loop.run_task(command)
        if registry.needs_confirmation_seen:
            return CONFIRMATION_REPLY, True
        return result.reply, False

    source = PwRecordSource(target=(config.mic_source_name or ""))
    listener = HandsFreeListener(
        config,
        source,
        reader=ContinuousReader(source),
        transcribe=transcribe,
        speak=speak,
        command_runner=run_command,
        save_debug=lambda pcm, tr, cmd: save_debug_attempt(WORKSPACE_DIR / "voice_debug", pcm, tr, cmd),
    )
    registry.on_execute = listener.note_tool_call
    return listener.run()
