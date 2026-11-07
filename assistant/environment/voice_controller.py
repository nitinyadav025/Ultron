"""Background voice controller for Ultron's standalone environment.

Runs without a terminal and supports three live modes:

* ``wake_word`` — "Ultron" (or a learned alias) opens an 8 s follow-up window.
* ``continuous`` — every reliable utterance goes to the brain without a wake word.
* ``ptt`` — the Dashboard calls ``start_ptt()`` / ``stop_ptt()``; no silence timeout.

The controller streams the microphone RMS/peak into :class:`EnvironmentState` for the
Dashboard waveform, and uses the existing deterministic detector, Whisper ears and Piper
mouth. Hardware is injectable so automated tests never open the real microphone.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

from assistant.config import AssistantConfig
from assistant.environment.state import EnvironmentState, get_state
from assistant.interfaces.listen import (
    UtteranceDetector,
    is_stop_command,
    pcm_to_float32,
)
from assistant.speech.ears import WhisperEars, has_real_word
from assistant.speech.mouth import PiperMouth
from assistant.speech.recorder import (
    FRAME_BYTES,
    PwRecordSource,
    RecorderError,
    frame_peak,
    frame_rms,
    pcm16_samples,
)
from assistant.speech.wake import (
    aliases_for,
    is_actionable_command,
    load_learned_aliases,
    normalise_spoken_command,
    strip_conversational_prefix,
    strip_wake_prefixes,
    wake_match,
    wake_match_anywhere,
)

logger = logging.getLogger("ultron.environment.voice")

BrainCallable = Callable[..., Any]


class EnvironmentVoiceController:
    """A managed daemon thread that turns microphone frames into brain commands."""

    def __init__(
        self,
        config: AssistantConfig,
        *,
        state: EnvironmentState | None = None,
        source_factory: Callable[[], Any] | None = None,
        ears: Any | None = None,
        mouth: Any | None = None,
        brain: BrainCallable | None = None,
        vad: Any | None = None,
        learned_aliases: list[str] | None = None,
        print_fn: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.state = state or get_state()
        self._source_factory = source_factory or self._default_source
        self.ears = ears
        self.mouth = mouth
        self.brain = brain
        self.vad = vad
        self.print_fn = print_fn or (lambda _line: None)
        self.clock = clock
        self.learned_aliases = (
            learned_aliases
            if learned_aliases is not None
            else load_learned_aliases(getattr(config, "wake_aliases_file", None))
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._source: Any | None = None
        self._detector: UtteranceDetector | None = None
        self._followup_until = 0.0
        self._ptt_active = threading.Event()
        self._ptt_stop = threading.Event()
        self._ptt_frames = bytearray()
        self._level_counter = 0
        self._was_muted = False

    # -- lifecycle ---------------------------------------------------------------

    def _default_source(self) -> Any:
        name = (getattr(self.config, "mic_source_name", "") or "").strip()
        return PwRecordSource(target=name) if name else PwRecordSource()

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ultron-environment-voice", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._ptt_active.clear()
        self._ptt_stop.set()
        source = self._source
        if source is not None:
            try:
                source.stop()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        if not self.state.snapshot()["tts_muted"]:
            self.state.set_status("online")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- live controls -----------------------------------------------------------

    def set_mode(self, mode: str) -> bool:
        if not self.state.set_voice_mode(mode):
            return False
        self._ptt_active.clear()
        self._ptt_stop.set()
        self._followup_until = 0.0
        self._detector = self._make_detector()
        return True

    def mute_mic(self, muted: bool) -> None:
        self.state.set_muted(mic=muted)
        if not muted and self.state.snapshot()["tts_muted"]:
            self.state.set_status("muted")

    def mute_tts(self, muted: bool) -> None:
        self.state.set_muted(tts=muted)
        if muted:
            self.barge_in()
        self.state.set_status("muted" if muted else "listening")

    def barge_in(self) -> bool:
        """Stop Piper playback immediately (Dashboard Stop, hotkey, or spoken interrupt)."""
        stopped = False
        mouth = self.mouth
        if mouth is not None and hasattr(mouth, "stop_speaking"):
            try:
                stopped = bool(mouth.stop_speaking())
            except Exception:
                stopped = False
        self._followup_until = 0.0
        self.state.set_status("listening")
        return stopped

    def start_ptt(self) -> None:
        self.state.set_voice_mode("ptt")
        self._ptt_frames.clear()
        self._ptt_stop.clear()
        self._ptt_active.set()
        self.state.set_status("listening")
        self.state.set_thought("Push-to-talk: recording…")

    def stop_ptt(self) -> None:
        self._ptt_active.clear()
        self._ptt_stop.set()

    def apply_config(self) -> None:
        """Apply the Dashboard's live config to the running controller."""
        live = self.state.get_config()
        for attr in (
            "voice_silence_ms",
            "voice_max_seconds",
            "listen_early_check_ms",
            "listen_followup_s",
        ):
            if attr in live:
                setattr(self.config, attr, live[attr])
        if "mic_source_volume" in live:
            setattr(self.config, "mic_source_volume", live["mic_source_volume"])
        self._detector = self._make_detector()

    # -- detector ----------------------------------------------------------------

    def _make_detector(self) -> UtteranceDetector:
        live = self.state.get_config()
        return UtteranceDetector(
            warmup_ms=getattr(self.config, "recorder_warmup_ms", 300),
            warmup_max_ms=getattr(self.config, "recorder_warmup_max_ms", 1500),
            min_snr_db=getattr(self.config, "voice_min_snr_db", 10.0),
            silence_ms=int(getattr(self.config, "voice_silence_ms", live["voice_silence_ms"])),
            early_ms=int(
                getattr(self.config, "listen_early_check_ms", live["listen_early_check_ms"])
            ),
            max_seconds=float(
                getattr(self.config, "voice_max_seconds", live["voice_max_seconds"])
            ),
            vad=self.vad,
        )

    # -- main loop ---------------------------------------------------------------

    def _run(self) -> None:
        self._detector = self._make_detector()
        self.state.set_status("listening")
        while not self._stop.is_set():
            source = self._source
            if source is None:
                try:
                    source = self._source_factory()
                    source.start()
                except (RecorderError, OSError) as exc:
                    self.state.set_status("error")
                    self.state.set_thought(f"Microphone unavailable: {exc}")
                    logger.warning("Environment microphone unavailable: %s", type(exc).__name__)
                    self._stop.wait(2.0)
                    continue
                self._source = source
                self.state.set_status("listening")

            try:
                chunk = source.read(FRAME_BYTES, timeout=0.25)
            except Exception as exc:
                logger.warning("Environment microphone read failed: %s", type(exc).__name__)
                self._close_source()
                continue
            if chunk is None:
                self._close_source()
                if self._stop.is_set():
                    break
                self._stop.wait(0.5)
                continue
            if not chunk:
                continue
            self._consume(chunk)

        self._close_source()
        if not self.state.snapshot()["tts_muted"]:
            self.state.set_status("online")

    def _close_source(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            try:
                source.stop()
            except Exception:
                pass

    def _consume(self, chunk: bytes) -> None:
        carry = chunk
        while len(carry) >= FRAME_BYTES and not self._stop.is_set():
            raw_frame, carry = carry[:FRAME_BYTES], carry[FRAME_BYTES:]
            self._publish_level(raw_frame)

            if self.state.snapshot()["mic_muted"]:
                self._was_muted = True
                continue
            if self._was_muted:
                # Re-arm cleanly after unmuting instead of replaying stale pre-roll.
                self._was_muted = False
                if self._detector is not None:
                    self._detector.reset()

            mode = self.state.snapshot()["voice_mode"]
            if mode == "ptt":
                if self._ptt_active.is_set():
                    self._ptt_frames.extend(raw_frame)
                elif self._ptt_stop.is_set():
                    self._ptt_stop.clear()
                    pcm = bytes(self._ptt_frames)
                    self._ptt_frames.clear()
                    if pcm:
                        self._handle_pcm(pcm, source="ptt")
                continue

            detector = self._detector
            if detector is None:
                continue
            event = detector.feed(pcm16_samples(raw_frame))
            if event == "final":
                pcm = detector.take_pcm()
                detector.reset()
                if pcm:
                    self._handle_pcm(pcm, source="voice")

    def _publish_level(self, raw_frame: bytes) -> None:
        self._level_counter += 1
        if self._level_counter % 2:
            return
        samples = pcm16_samples(raw_frame)
        self.state.set_audio_levels(frame_rms(samples), frame_peak(samples))

    # -- utterance handling ------------------------------------------------------

    def _handle_pcm(self, pcm: bytes, *, source: str) -> None:
        if self.state.snapshot()["mic_muted"]:
            return
        self.state.set_status("transcribing")
        self.state.set_thought(
            f"Transcribing with faster-whisper {getattr(self.config, 'whisper_model', 'small.en')}…"
        )
        started = self.clock()
        text = self._transcribe(pcm)
        transcribe_ms = (self.clock() - started) * 1000.0
        if not text:
            self.state.set_status("listening")
            self.state.set_thought("Heard noise, ignored.")
            return

        self.state.set_transcript(text)
        self.state.add_turn("user", text, source=source)
        handled = self._resolve_command(text, source=source, transcribe_ms=transcribe_ms)
        if not handled:
            self.state.set_status("listening")

    def _transcribe(self, pcm: bytes) -> str:
        ears = self.ears
        if ears is None:
            try:
                ears = WhisperEars(self.config)
                self.ears = ears
            except Exception as exc:
                logger.warning("Ears unavailable: %s", type(exc).__name__)
                return ""
        samples = pcm_to_float32(pcm)
        try:
            ok, text = ears.transcribe_samples(samples, log_text=False, vad_filter=True)
        except Exception as exc:
            logger.warning("Transcription failed: %s", type(exc).__name__)
            return ""
        if not ok:
            return ""
        cleaned = normalise_spoken_command(text)
        if not has_real_word(cleaned):
            return ""
        # WhisperEars already ran the same guard behind transcript_is_unreliable(); its
        # verdict keeps rejected/noise decodes out of continuous mode.
        verdict = str(getattr(ears, "last_confidence", {}).get("verdict") or "ok")
        if verdict != "ok":
            logger.info("Environment voice rejected a transcript: %s", verdict)
            return ""
        return cleaned

    def _resolve_command(self, text: str, *, source: str, transcribe_ms: float) -> bool:
        mode = self.state.snapshot()["voice_mode"]
        now = self.clock()
        command = text
        wake_found = False

        if mode == "wake_word":
            match = wake_match(
                text,
                getattr(self.config, "wake_word", "ultron"),
                getattr(self.config, "wake_aliases", ""),
                self.learned_aliases,
            )
            if not match.found:
                anywhere = wake_match_anywhere(
                    text,
                    getattr(self.config, "wake_word", "ultron"),
                    getattr(self.config, "wake_aliases", ""),
                    self.learned_aliases,
                )
                if anywhere.found and anywhere.command:
                    match = anywhere
            wake_found = match.found
            in_followup = now < self._followup_until
            if not wake_found and not in_followup:
                # A clear spoken command must never be dropped just because "Ultron"
                # was not said ("Open Firefox", "What is on my screen"). The stripped,
                # high-confidence utterance goes straight to the brain.
                if is_actionable_command(
                    text,
                    getattr(self.config, "wake_word", "ultron"),
                    getattr(self.config, "wake_aliases", ""),
                    self.learned_aliases,
                ):
                    command = text
                    self.state.set_thought("Direct command (no wake word).")
                    logger.info("Environment accepted a direct command without the wake word")
                else:
                    self.state.set_thought("Ignored speech (no wake word).")
                    return False
            elif wake_found:
                command = match.command
                if not command.strip():
                    self._say("Yes?")
                    self._followup_until = now + float(
                        getattr(self.config, "listen_followup_s", 8.0)
                    )
                    self.state.set_status("listening")
                    return True
            else:
                command = strip_wake_prefixes(
                    command,
                    getattr(self.config, "wake_word", "ultron"),
                    getattr(self.config, "wake_aliases", ""),
                    self.learned_aliases,
                )
        elif mode == "continuous":
            command = self._without_leading_wake(text)
        else:  # ptt
            command = self._without_leading_wake(text)

        # Remove polite/conversational lead-ins for every mode so the fast path sees
        # "open firefox" and not "I want you to open firefox".
        command = strip_conversational_prefix(
            command,
            getattr(self.config, "wake_word", "ultron"),
            getattr(self.config, "wake_aliases", ""),
            self.learned_aliases,
        )
        command = normalise_spoken_command(command).strip(" ,.")
        if not command:
            self.state.set_status("listening")
            return False
        if is_stop_command(command):
            self._say("Stopping.")
            self.state.set_thought("Stop command received.")
            self.stop()
            return True

        self.state.set_thought(f"Thinking about: {command}")
        self.state.set_status("thinking")
        started = self.clock()
        reply, tools_called, llm_ms, tool_ms = self._run_brain(command, source)
        total_ms = (self.clock() - started) * 1000.0
        self.state.add_latency(
            transcribe_ms=transcribe_ms,
            llm_ms=llm_ms,
            tool_ms=tool_ms,
            total_ms=transcribe_ms + total_ms,
        )
        if reply:
            self.state.add_turn(
                "assistant",
                reply,
                tools_called=tools_called,
                latency_ms=transcribe_ms + total_ms,
                source=source,
            )
            self.state.set_reply(reply)
            if not self.state.snapshot()["tts_muted"]:
                self._say(reply)
        self.state.set_status("listening")
        if mode == "wake_word":
            # Only wake-word mode has a follow-up window; continuous/PTT need none.
            self._followup_until = now + float(getattr(self.config, "listen_followup_s", 8.0))
        return True

    def _without_leading_wake(self, text: str) -> str:
        """Continuous/PTT: only a wake spelling at the *start* is removed.

        ``wake_match`` can fuzzy-match two words anywhere ("hello Ultron" ~ "hey Ultron"),
        which would empty a normal sentence that merely ends with the name.
        """
        tokens = re.findall(r"[a-z0-9']+", (text or "").lower())
        if not tokens:
            return text
        aliases = aliases_for(
            getattr(self.config, "wake_word", "ultron"),
            getattr(self.config, "wake_aliases", ""),
            self.learned_aliases,
        )
        one_word = {alias for alias in aliases if len(alias.split()) == 1}
        two_words = {alias for alias in aliases if len(alias.split()) == 2}
        starts = tokens[0] in one_word
        if not starts and len(tokens) >= 2:
            starts = f"{tokens[0]} {tokens[1]}" in two_words
        if not starts:
            return text
        return strip_wake_prefixes(
            text,
            getattr(self.config, "wake_word", "ultron"),
            getattr(self.config, "wake_aliases", ""),
            self.learned_aliases,
        ) or text

    def _run_brain(self, command: str, source: str) -> tuple[str, list[str], float, float]:
        if self.brain is None:
            return "", [], 0.0, 0.0
        started = self.clock()
        try:
            result = self.brain(command, source=source)
        except TypeError:
            result = self.brain(command)
        except Exception as exc:
            logger.warning("Environment brain failed: %s", type(exc).__name__)
            self.state.set_thought(f"Brain error: {type(exc).__name__}")
            return "", [], (self.clock() - started) * 1000.0, 0.0
        elapsed = (self.clock() - started) * 1000.0
        if isinstance(result, str):
            return result, [], elapsed, 0.0
        if not isinstance(result, dict):
            return "", [], elapsed, 0.0
        reply = str(result.get("reply") or "")
        tools = [str(tool) for tool in (result.get("tools_called") or [])]
        llm_ms = float(result.get("llm_ms") or result.get("latency_ms") or elapsed)
        tool_ms = float(result.get("tool_ms") or 0.0)
        return reply, tools, llm_ms, tool_ms

    def _say(self, text: str) -> None:
        mouth = self.mouth
        if mouth is None:
            return
        try:
            if hasattr(mouth, "speak"):
                mouth.speak(text)
            elif hasattr(mouth, "synthesize_wav"):
                mouth.synthesize_wav(text)
        except Exception as exc:
            logger.warning("Environment mouth failed: %s", type(exc).__name__)
