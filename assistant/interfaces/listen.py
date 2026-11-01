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
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from assistant.config import AssistantConfig
from assistant.speech.recorder import (
    CALIBRATION_MS,
    DEFAULT_NOISE_FLOOR,
    DEFAULT_WARMUP_MS,
    FRAME_BYTES,
    FRAME_MS,
    MIN_NOISE_FLOOR,
    PRE_ROLL_MS,
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
from assistant.speech.wake import first_word, sentence_is_complete, split_wake_word
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
        min_snr_db: float = 10.0,
        silence_ms: int = 2000,
        early_ms: int = 800,
        max_seconds: float = 30.0,
    ) -> None:
        self.warmup_ms = int(warmup_ms)
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
        self._speech_run = 0
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
        self._speech_run = 0
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
        if self.audio_ms <= self.warmup_ms:
            return None

        level = frame_rms(frame)
        peak = frame_peak(frame)
        burst = _is_clipped_burst(level, peak)

        if not self._calibrated:
            if not burst:
                self._calibration.append(level)
            if self.audio_ms >= self.warmup_ms + CALIBRATION_MS:
                estimate = float(percentile(self._calibration, 0.25)) if self._calibration else DEFAULT_NOISE_FLOOR
                self.noise_floor = max(MIN_NOISE_FLOOR, estimate)
                self._calibrated = True

        start_thr, end_thr = self._thresholds()
        is_speech = level >= start_thr and not burst

        if not self._in_speech:
            self._ring.append(frame)
            if is_speech:
                self._speech_run += 1
                self._speech_frames += 1
            else:
                self._speech_run = 0
                if self._calibrated and level < start_thr:
                    self.noise_floor = max(MIN_NOISE_FLOOR, 0.9 * self.noise_floor + 0.1 * level)
            if self._speech_run * FRAME_MS >= MIN_SPEECH_MS:
                self._in_speech = True
                self._start_ms = max(0, self.audio_ms - self._speech_run * FRAME_MS)
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

    @property
    def speech_ms(self) -> int:
        return self._speech_frames * FRAME_MS


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
        save_debug: Callable[[bytes, Transcript, str], None] | None = None,
        print_fn: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.source = source
        self.transcribe = transcribe
        self.speak = speak
        self.command_runner = command_runner
        self.save_debug = save_debug
        self.print_fn = print_fn
        self.clock = clock
        self.detector = UtteranceDetector(
            warmup_ms=getattr(config, "recorder_warmup_ms", DEFAULT_WARMUP_MS),
            min_snr_db=getattr(config, "voice_min_snr_db", 10.0),
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
        self.print_fn(START_LINE)
        try:
            self.source.start()
            while not self.stop_requested:
                chunk = self.source.read(FRAME_BYTES, timeout=0.25)
                if chunk is None:
                    break
                if not chunk:
                    continue
                self._consume(chunk)
        except KeyboardInterrupt:
            logger.info("Hands-free listening stopped by Ctrl+C.")
        finally:
            self.source.stop()
        return 0

    # -- internals ----------------------------------------------------------------

    def _consume(self, chunk: bytes) -> None:
        self._carry += chunk
        while len(self._carry) >= FRAME_BYTES and not self.stop_requested:
            raw_frame, self._carry = self._carry[:FRAME_BYTES], self._carry[FRAME_BYTES:]
            frame = pcm16_samples(raw_frame)
            if self.detector.audio_ms < self.ignore_until_ms:
                self.detector.skip(frame)
                continue
            event = self.detector.feed(frame)
            if event == "early":
                self._handle_early()
            elif event == "final":
                self._handle_final()
            elif event == "noise":
                logger.info("Hands-free: steady noise ignored.")

    def _transcribe(self, pcm: bytes) -> Transcript:
        return self.transcribe(pcm)

    def _handle_early(self) -> None:
        self._t_end = self.clock()
        pcm = self.detector.take_pcm(trim_tail=False)
        self._t_endpoint = self.clock()
        transcript = self._transcribe(pcm)
        self._t_trans = self.clock()
        logger.info("Hands-free: early transcript in %.2fs: %r", self._t_trans - self._t_endpoint, transcript.text)
        if not transcript.ok:
            # A rejected decode cannot be trusted either way; process it now so a wake word
            # that is still readable in the raw text gets "Sorry, say that again.".
            self._process(transcript, pcm)
        elif sentence_is_complete(transcript.text):
            self._process(transcript, pcm)
        else:
            # A dangling word: keep listening, do not send this half-sentence anywhere.
            logger.info("Hands-free: sentence not finished yet (dangling word).")

    def _handle_final(self) -> None:
        self._t_end = self.clock()
        pcm = self.detector.take_pcm()
        self._t_endpoint = self.clock()
        transcript = self._transcribe(pcm)
        self._t_trans = self.clock()
        self._process(transcript, pcm)

    def _process(self, transcript: Transcript, pcm: bytes) -> None:
        started = self.detector.last_start_ms
        in_followup = started <= self.followup_until_ms
        wake_text = transcript.raw or transcript.text
        wake_found, command = split_wake_word(
            wake_text,
            getattr(self.config, "wake_word", "ultron"),
            getattr(self.config, "wake_aliases", ""),
        )
        if not wake_found and in_followup:
            wake_found = True
            command = re.sub(r"\s+", " ", transcript.text or "").strip()

        if not wake_found:
            self.ignored += 1
            if getattr(self.config, "listen_log_ignored", False):
                logger.info("Hands-free: ignored speech starting with %r", first_word(wake_text))
            self.detector.reset()
            return

        if not transcript.ok:
            self.detector.reset()
            self._speak(RETRY_REPLY)
            return

        if not command:
            self.detector.reset()
            self._speak(YES_REPLY)
            return

        if command.strip(" .!?").lower() in STOP_COMMANDS:
            self.detector.reset()
            self.stop_requested = True
            return

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
        self.print_fn(f"You: {command} | Ultron: {reply}")
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
        # Anything the mic picked up while Piper talked is discarded.
        self._drain()
        self.ignore_until_ms = self.detector.audio_ms + int(getattr(self.config, "listen_after_speech_ms", 600))
        self.followup_until_ms = self.ignore_until_ms + int(getattr(self.config, "listen_followup_s", 6.0) * 1000)
        logger.info(
            "Hands-free: speaking done; ignoring %.0f ms, wake-free window %.0f ms",
            getattr(self.config, "listen_after_speech_ms", 600),
            getattr(self.config, "listen_followup_s", 6.0) * 1000,
        )

    def _drain(self) -> None:
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
    # Apply the optional mic level silently: `listen` prints only the start line and
    # one line per accepted command.
    apply_mic_source_volume(config.mic_source_volume)

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
    ok_mouth, mouth_msg = voice.load_mouth()
    if not ok_mouth:
        print(f"[ERROR] Mouth unavailable: {mouth_msg}", flush=True)
        return 1

    def transcribe(pcm: bytes) -> Transcript:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        ok, text = voice.ears.transcribe_samples(samples)
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
        transcribe=transcribe,
        speak=speak,
        command_runner=run_command,
        save_debug=lambda pcm, tr, cmd: save_debug_attempt(WORKSPACE_DIR / "voice_debug", pcm, tr, cmd),
    )
    registry.on_execute = listener.note_tool_call
    return listener.run()
