"""Voice pipeline for Ultron on Fedora 44 (PipeWire mic + local ears + cloud or local TTS).

BACKEND=local: microphone recording -> faster-whisper `base.en` on the CPU (int8, beam 1,
VAD on) for transcription; Gemini Live voice and Gemini TTS are not used.
BACKEND=gemini: the original cloud transcription/TTS path, unchanged.
"""

from __future__ import annotations

import array
import concurrent.futures
import io
import json
import logging
import math
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from google.genai import types

from assistant.brain.gemini_client import GeminiAdapter
from assistant.config import WORKSPACE_DIR, AssistantConfig, clamp_voice_volume
from assistant.safety.redact import redact_secrets
from assistant.speech.denoise import denoise_pcm16
from assistant.speech.ears import EarsError, WhisperEars
from assistant.speech.mouth import MouthError, PiperMouth, split_sentences
from assistant.speech.recorder import (
    MIN_SPEECH_MS,
    PwRecordSource,
    RecorderError,
    debug_level_summary,
    measure_peak_rms,
    normalise_mic_level,
    record_for_duration,
    record_until_silence,
    trim_leading_transient,
    write_pcm16_wav,
)

logger = logging.getLogger("ultron.voice")

TRANSCRIBE_PROMPT = (
    "Transcribe exactly. Output only the spoken words. "
    "If there is no speech, output exactly [NO_SPEECH]."
)

NOTHING_HEARD = "I didn't hear anything."


def extract_transcript_text(resp: types.GenerateContentResponse) -> str:
    """Extract transcript text, preferring explicit part accessors over resp.text.

    Dedicated transcribe models return the transcript in an ``audio_transcription``
    part, which the SDK's ``.text`` shortcut ignores (it concatenates only plain
    text parts). Fall back to resp.text for chat-style models.
    """
    chunks: list[str] = []
    candidates = getattr(resp, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if not text:
                at = getattr(part, "audio_transcription", None)
                if at is not None:
                    # Dedicated transcribe models wrap the transcript in a Transcription object.
                    text = getattr(at, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
    return "".join(chunks).strip() or (getattr(resp, "text", None) or "").strip()


def _terminate_process(proc: subprocess.Popen[bytes] | subprocess.Popen[str] | None) -> None:
    """Terminate a child process, escalating to kill if it will not exit within 1s."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

# -3 dBFS target peak for 16-bit signed PCM (32767 * 10^(-3/20) ≈ 23197)
TARGET_PEAK_MINUS_3DBFS = 23197
MIN_SPEECH_PEAK = 300
MIN_SPEECH_RMS = 60
# Microphone recordings are normalised to 70% of full scale: loud/clipped input is turned
# DOWN, quiet input is boosted (max 8x), so Whisper always gets a clean, even level.
SAFE_MIC_PEAK_RATIO = 0.7
SAFE_MIC_PEAK = int(round(32767 * SAFE_MIC_PEAK_RATIO))
MAX_MIC_BOOST = 8.0


def compute_pcm16_peak_rms(pcm_bytes: bytes) -> tuple[int, int]:
    """Calculate (peak, rms) for 16-bit signed little-endian PCM bytes using array (Python 3.14 safe)."""
    usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable_len <= 0:
        return 0, 0
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:usable_len])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0, 0
    peak = 0
    sum_sq = 0
    for s in samples:
        a = abs(s)
        if a > peak:
            peak = a
        sum_sq += s * s
    rms = int(math.sqrt(sum_sq / len(samples)))
    return peak, rms


def scale_pcm16_bytes(
    pcm_bytes: bytes,
    target_peak: int = TARGET_PEAK_MINUS_3DBFS,
    volume_gain: float = 1.0,
    max_auto_gain: float = 10.0,
) -> bytes:
    """Normalize 16-bit PCM bytes toward target_peak * volume_gain with hard clipping protection."""
    usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable_len <= 0:
        return b""
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:usable_len])
    if sys.byteorder != "little":
        samples.byteswap()

    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return pcm_bytes[:usable_len]

    clamped_vol = clamp_voice_volume(volume_gain)
    base_gain = min(max_auto_gain, float(target_peak) / float(peak))
    total_gain = base_gain * clamped_vol

    if abs(total_gain - 1.0) < 0.02:
        return pcm_bytes[:usable_len]

    out = array.array("h", (max(-32768, min(32767, int(round(s * total_gain)))) for s in samples))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def ensure_wav_bytes(
    raw_or_wav: bytes,
    default_rate: int = 24000,
    default_channels: int = 1,
    normalize_to_minus_3dbfs: bool = True,
    volume_gain: float = 1.0,
) -> bytes:
    """Accept either RIFF WAV bytes or raw 16-bit 24kHz mono PCM bytes and return normalized WAV bytes."""
    if not raw_or_wav:
        raise ValueError("Empty audio payload received.")

    if raw_or_wav.startswith(b"RIFF"):
        with wave.open(io.BytesIO(raw_or_wav), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            pcm_frames = wf.readframes(wf.getnframes())
    else:
        channels = default_channels
        sampwidth = 2
        rate = default_rate
        pcm_frames = raw_or_wav

    if sampwidth == 2 and normalize_to_minus_3dbfs:
        pcm_frames = scale_pcm16_bytes(
            pcm_frames,
            target_peak=TARGET_PEAK_MINUS_3DBFS,
            volume_gain=volume_gain,
            max_auto_gain=12.0,
        )

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf_out:
        wf_out.setnchannels(channels)
        wf_out.setsampwidth(sampwidth)
        wf_out.setframerate(rate)
        wf_out.writeframes(pcm_frames)
    return buf.getvalue()


def clean_speech_text(text: str, max_chars: int = 220) -> str:
    """Shorten a model reply into 1-2 natural spoken sentences (~220 chars max), stripping code/markdown/paths."""
    if not text:
        return ""
    cleaned = text
    # Strip untrusted tool output blocks and fenced code blocks
    cleaned = re.sub(r"<untrusted_tool_output[^>]*>.*?</untrusted_tool_output>", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"```.*?```", " ", cleaned, flags=re.DOTALL)
    # Strip JSON objects/arrays
    cleaned = re.sub(r"\{[^{}]*\}", " ", cleaned)
    # Strip markdown links [label](url) -> label
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    # Strip inline code and file paths (/home/..., ./..., ~/...)
    cleaned = re.sub(r"`[^`]+`", " ", cleaned)
    cleaned = re.sub(r"(?:~|\.|/[a-zA-Z0-9_\-\.]+)+/[a-zA-Z0-9_\-\./]*", " ", cleaned)
    # Strip markdown formatting symbols and bullet prefixes
    cleaned = re.sub(r"(?m)^\s*[-*#>+]+\s*", "", cleaned)
    cleaned = re.sub(r"[*_~#`>|]", "", cleaned)
    # Tidy empty slots left behind by stripped paths/code so speech does not say "like , , , plus".
    cleaned = re.sub(r"\(\s*\)", " ", cleaned)
    cleaned = re.sub(r"\b(?:like|such as|including)\s*(?=[,.;:!?])", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*,\s*([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"(\s*,\s*)+", ", ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")

    if not cleaned:
        return "Done."

    # Keep at most 1-2 natural sentences
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    selected = " ".join(sentences[:2]) if sentences else cleaned

    if len(selected) > max_chars:
        cut = selected[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-")
        selected = (cut or selected[:max_chars]) + "."
    return selected


class VoiceController:
    """Manages PipeWire recording, local (faster-whisper) or cloud transcription, and TTS playback."""

    def __init__(
        self,
        config: AssistantConfig,
        adapter: GeminiAdapter | None,
        status_callback: Callable[[str], None] | None = None,
        transcribe_timeout_sec: float = 20.0,
        vad: Any | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.status_callback = status_callback or (lambda _msg: None)
        self.transcribe_timeout_sec = transcribe_timeout_sec
        # Production passes the Silero wrapper; unit tests leave it None so a synthetic
        # tone is judged by the energy rules (deterministic and fast).
        self.vad = vad
        # Local ears exist only in local mode; the model itself loads once via load_ears().
        self.ears: WhisperEars | None = WhisperEars(config) if config.backend == "local" else None
        # Local mouth (Piper) replaces Gemini TTS + spd-say entirely in local mode.
        self.mouth: PiperMouth | None = PiperMouth(config) if config.backend == "local" else None
        self.audio_file: Path = WORKSPACE_DIR / "voice_input.wav"
        self.tts_file: Path = WORKSPACE_DIR / "voice_output.wav"
        self.last_peak: int = 0
        self.last_rms: int = 0
        self.last_peak_before: int = 0
        self.last_rms_before: int = 0
        self._clipping_warned: bool = False
        self._last_debug_json: Path | None = None
        self._last_debug_trace: dict = {}
        self.last_record_reason: str = ""
        self.last_noise_floor: float = 0.0
        self.debug_dir: Path = WORKSPACE_DIR / "voice_debug"
        self.active_tts_engine: str = ""
        self._announced_engine: str = ""
        self._play_lock = threading.Lock()
        self._play_proc: subprocess.Popen[bytes] | subprocess.Popen[str] | None = None
        self._speech_procs: list[subprocess.Popen[bytes] | subprocess.Popen[str]] = []
        # Monotonic time until which cloud TTS is skipped after a failure, so a dead
        # engine (quota/slow API) never stalls spoken replies with repeated 20s waits.
        self._cloud_tts_retry_after: float = 0.0

    def _notify_engine_once(self, engine_label: str) -> None:
        """Update the engine label and show "Speaking (...)" for every spoken reply.

        The log line stays once-per-engine (it is a startup style fact), but the screen
        line is printed every time: in `/voice` the user must see that Piper is talking
        even when the sentence itself is unchanged.
        """
        self.active_tts_engine = engine_label
        if self._announced_engine != engine_label:
            self._announced_engine = engine_label
            logger.info("Voice TTS engine active: %s", engine_label)
        self.status_callback(f"Speaking ({engine_label})...")

    def stop_speaking(self) -> None:
        """Kill every playing TTS process immediately so multiple voices never overlap."""
        with self._play_lock:
            procs = list(self._speech_procs)
            self._speech_procs.clear()
            legacy = self._play_proc
            self._play_proc = None
        _terminate_process(legacy)
        for proc in procs:
            _terminate_process(proc)

    def _wait_for_playback(self, timeout: float = 1.5) -> None:
        """Block until no TTS player is alive: Ultron must never record its own voice."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._play_lock:
                procs = [p for p in [self._play_proc, *self._speech_procs] if p is not None]
            if not any(p.poll() is None for p in procs):
                return
            time.sleep(0.05)

    def record(self, duration_sec: float | None = None) -> tuple[bool, str]:
        """Record one spoken request: listen until the user stops talking (VOICE_MODE=auto).

        VOICE_MODE=fixed keeps the old behaviour: record exactly VOICE_FIXED_SECONDS.
        """
        mode = (getattr(self.config, "voice_mode", "auto") or "auto").lower()
        if mode == "fixed":
            seconds = duration_sec if duration_sec is not None else self.config.voice_fixed_seconds
            return self.record_push_to_talk(seconds)
        return self.record_auto()

    def _new_source(self) -> PwRecordSource:
        """Build the mic source, using MIC_SOURCE_NAME (a PipeWire filter source) if set."""
        name = (getattr(self.config, "mic_source_name", "") or "").strip()
        return PwRecordSource(target=name) if name else PwRecordSource()

    def record_auto(self) -> tuple[bool, str]:
        """Wait for speech, keep a little pre-roll, stop on silence, and never send silence to Whisper."""
        self.stop_speaking()
        self._wait_for_playback()
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        if self.audio_file.exists():
            self.audio_file.unlink(missing_ok=True)

        source = self._new_source()
        try:
            recording = record_until_silence(
                source,
                start_timeout_s=getattr(self.config, "voice_start_timeout_s", 10.0),
                silence_ms=getattr(
                    self.config,
                    "voice_record_silence_ms",
                    getattr(self.config, "voice_silence_ms", 1350),
                ),
                max_seconds=getattr(self.config, "voice_max_seconds", 12.0),
                warmup_ms=getattr(self.config, "recorder_warmup_ms", 300),
                warmup_max_ms=getattr(self.config, "recorder_warmup_max_ms", 1500),
                min_snr_db=getattr(self.config, "voice_min_snr_db", 5.0),
                status=self.status_callback,
                vad=self.vad,
            )
        except KeyboardInterrupt:
            # Ctrl+C while listening cancels only this capture; the chat stays open.
            logger.info("Listening cancelled by Ctrl+C.")
            self.status_callback("Cancelled.")
            return False, "cancelled"
        except RecorderError as exc:
            msg = redact_secrets(str(exc))
            logger.error("Recording failed: %s", msg)
            return False, msg
        finally:
            source.stop()

        if getattr(source, "error", ""):
            logger.info("pw-record stderr: %s", redact_secrets(source.error))
        logger.info(
            "Auto recording: %s, %.1fs captured (%.1fs speech)",
            recording.reason,
            recording.duration_ms / 1000.0,
            recording.speech_ms / 1000.0,
        )
        self._save_debug_attempt(recording, "auto")

        if not recording.saw_speech:
            if recording.reason == "cancelled":
                self.status_callback("Cancelled.")
                return False, "cancelled"
            if recording.reason == "no_audio":
                return False, "Microphone produced no audio."
            # Silence, a fan, or a capped recording with no voice: nothing was said.
            return False, NOTHING_HEARD
        if recording.speech_ms < MIN_SPEECH_MS:
            return False, NOTHING_HEARD
        self.last_record_reason = recording.reason
        noise_floor = float((recording.trace.get("thresholds") or {}).get("noise_floor") or 0.0)
        return self._finish_recording(recording.pcm, 16000, 1, has_speech=True, noise_floor=noise_floor)

    def _finish_recording(
        self, pcm: bytes, rate: int, channels: int, *, has_speech: bool, noise_floor: float = 0.0
    ) -> tuple[bool, str]:
        """Drop the mic wake-up thump, normalise on a robust level, save the WAV, log levels."""
        trimmed_pcm, dropped_ms = trim_leading_transient(pcm)
        if dropped_ms:
            logger.info("Dropped %d ms of mic wake-up thump at the start of the recording.", dropped_ms)
        pcm = trimmed_pcm

        strength = float(getattr(self.config, "voice_denoise_strength", 0.0) or 0.0)
        if has_speech and strength > 0:
            highpass_hz = float(getattr(self.config, "voice_highpass_hz", 100.0) or 0.0)
            pcm = denoise_pcm16(pcm, strength=strength, highpass_hz=highpass_hz, rate=rate)
            logger.info("Denoise: high-pass %.0f Hz + gate strength %.2f", highpass_hz, strength)

        before_peak, before_rms = measure_peak_rms(pcm)
        self.last_peak_before, self.last_rms_before = before_peak, before_rms
        self.last_peak, self.last_rms = before_peak, before_rms

        if getattr(self.config, "voice_debug", False):
            write_pcm16_wav(WORKSPACE_DIR / "voice_input_raw.wav", pcm, rate=rate, channels=channels)
            print("  [voice-debug] raw recording saved to workspace/voice_input_raw.wav", flush=True)
            for line in debug_level_summary(pcm):
                print(f"  [voice-debug] {line}", flush=True)

        normalised, info = normalise_mic_level(pcm, has_speech=has_speech)
        self.last_peak, self.last_rms = info.after_peak, info.after_rms
        write_pcm16_wav(self.audio_file, normalised, rate=rate, channels=channels)

        if info.clipping and not self._clipping_warned:
            self._clipping_warned = True
            logger.warning(
                "Mic is clipping: %d samples at full scale. Lower the mic input slightly, "
                "about 20 percent lower than the current value. Check the current value with: "
                "wpctl get-volume @DEFAULT_AUDIO_SOURCE@",
                info.clipping_samples,
            )
        self.last_noise_floor = noise_floor
        snr_db = 0.0
        if noise_floor > 0 and info.speech_level > 0:
            snr_db = 20.0 * math.log10(info.speech_level / noise_floor)
        logger.info(
            "Mic level: before peak=%d rms=%d -> after peak=%d rms=%d "
            "(speech level %d, gain %.2fx, noise floor %d, snr %.1f dB%s)",
            info.before_peak,
            info.before_rms,
            info.after_peak,
            info.after_rms,
            info.speech_level,
            info.gain,
            int(noise_floor),
            snr_db,
            ", no speech" if not has_speech else "",
        )
        return True, str(self.audio_file)

    # ----------------------------------------------------------------------------------
    # Debug capture: one WAV + one JSON trace per listening attempt (newest 10 kept)
    # ----------------------------------------------------------------------------------

    def _save_debug_attempt(self, recording: object, mode: str) -> None:
        """Save the raw WAV and the level trace for this attempt; never break listening."""
        try:
            directory = self.debug_dir
            directory.mkdir(parents=True, exist_ok=True)
            now = datetime.now()
            stem = f"attempt_{now:%Y%m%d-%H%M%S-%f}"
            pcm = getattr(recording, "debug_pcm", b"") or getattr(recording, "pcm", b"")
            if pcm:
                write_pcm16_wav(directory / f"{stem}.wav", pcm, rate=16000, channels=1)
            trace = {
                "timestamp": now.isoformat(timespec="seconds"),
                "mode": mode,
                "reason": getattr(recording, "reason", "error"),
                "saw_speech": bool(getattr(recording, "saw_speech", False)),
                "duration_ms": int(getattr(recording, "duration_ms", 0)),
                "speech_ms": int(getattr(recording, "speech_ms", 0)),
                "transcript": None,
                "guard": None,
                **dict(getattr(recording, "trace", {}) or {}),
            }
            json_path = directory / f"{stem}.json"
            json_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
            self._prune_debug_attempts(directory, keep=10)
            self._last_debug_json = json_path
            self._last_debug_trace = trace
            if not trace["saw_speech"]:
                self._log_attempt(trace)
        except OSError as exc:  # a full disk must never break the microphone path
            logger.warning("Could not save voice debug capture: %s", exc)

    @staticmethod
    def _prune_debug_attempts(directory: Path, keep: int) -> None:
        stems = sorted({path.stem for path in directory.glob("attempt_*")}, reverse=True)
        for old in stems[keep:]:
            for suffix in (".wav", ".json"):
                (directory / f"{old}{suffix}").unlink(missing_ok=True)

    def _attach_debug_transcript(self, transcript: str) -> None:
        """Add Whisper text + guard scores to the last attempt JSON, then log one line."""
        if not self._last_debug_trace:
            return
        trace = dict(self._last_debug_trace)
        confidence = dict(getattr(self.ears, "last_confidence", {}) or {}) if self.ears else {}
        trace["transcript"] = transcript
        trace["guard"] = confidence or None
        if self._last_debug_json is not None:
            try:
                self._last_debug_json.write_text(
                    json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as exc:
                logger.warning("Could not update voice debug capture: %s", exc)
        self._log_attempt(trace)
        self._last_debug_json = None
        self._last_debug_trace = {}

    def _log_attempt(self, trace: dict) -> None:
        thresholds = trace.get("thresholds") or {}
        guard = trace.get("guard") or {}

        def score(value: object) -> str:
            return "n/a" if value is None else f"{float(value):.3f}"

        logger.info(
            "Listening attempt: mode=%s reason=%s duration=%.1fs speech=%.1fs speech_start=%sms "
            "noise=%s start_thr=%s end_thr=%s text=%r guard=%s(avg=%s,ns=%s,cr=%s)",
            trace.get("mode"),
            trace.get("reason"),
            float(trace.get("duration_ms") or 0) / 1000.0,
            float(trace.get("speech_ms") or 0) / 1000.0,
            trace.get("speech_start_ms", 0),
            thresholds.get("noise_floor"),
            thresholds.get("start"),
            thresholds.get("end"),
            trace.get("transcript"),
            guard.get("verdict", "-"),
            score(guard.get("min_avg_logprob")),
            score(guard.get("max_no_speech_prob")),
            score(guard.get("max_compression_ratio")),
        )

    def record_push_to_talk(self, duration_sec: float = 5.0) -> tuple[bool, str]:
        """Fixed-window mode over the same streaming source auto mode uses.

        The old path wrote a WAV file and stopped pw-record with SIGINT; it could keep only
        the first word. Streaming + record_for_duration keeps the whole window.
        """
        self.stop_speaking()
        self._wait_for_playback()
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        if self.audio_file.exists():
            self.audio_file.unlink(missing_ok=True)

        last_fail = "Recording failed."
        for attempt in (1, 2):
            source = self._new_source()
            try:
                recording = record_for_duration(
                    source,
                    max(0.5, float(duration_sec)),
                    status=self.status_callback,
                    warmup_ms=getattr(self.config, "recorder_warmup_ms", 300),
                    warmup_max_ms=getattr(self.config, "recorder_warmup_max_ms", 1500),
                )
            except KeyboardInterrupt:
                logger.info("Fixed recording cancelled by Ctrl+C.")
                self.status_callback("Cancelled.")
                return False, "cancelled"
            except RecorderError as exc:
                last_fail = redact_secrets(str(exc))
                logger.error("Recording failed: %s", last_fail)
            else:
                if getattr(source, "error", ""):
                    logger.info("pw-record stderr: %s", redact_secrets(source.error))
                pcm = recording.pcm
                peak, rms = measure_peak_rms(pcm)
                logger.info(
                    "Fixed recording: %s, %.1fs captured (peak=%d, rms=%d)",
                    recording.reason,
                    recording.duration_ms / 1000.0,
                    peak,
                    rms,
                )
                self._save_debug_attempt(recording, "fixed")
                if not pcm or peak < MIN_SPEECH_PEAK or rms < MIN_SPEECH_RMS:
                    msg = "mic is too quiet or muted"
                    logger.warning("Voice check failed: %s (peak=%d, rms=%d)", msg, peak, rms)
                    return False, msg
                return self._finish_recording(pcm, 16000, 1, has_speech=recording.saw_speech)
            finally:
                source.stop()

            if attempt == 1:
                logger.warning("Recording attempt 1 failed (%s); retrying once in 0.5s.", last_fail)
                time.sleep(0.5)
        return False, last_fail

    def _candidate_transcribe_models(self) -> list[str]:
        """Return ordered unique fallback list: transcribe -> fast -> main."""
        ordered: list[str] = []
        for role in ("transcribe", "fast", "main"):
            m = (self.config.models.get(role) or "").strip()
            if m and m not in ordered:
                ordered.append(m)
        return ordered

    def _call_transcribe_once(self, model_id: str, audio_bytes: bytes) -> str:
        """Execute a single generate_content transcription call with a hard timeout."""
        cfg = types.GenerateContentConfig(
            http_options=types.HttpOptions(timeout=int(self.transcribe_timeout_sec * 1000)),
        )
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                    types.Part.from_text(text=TRANSCRIBE_PROMPT),
                ],
            )
        ]

        def _invoke() -> types.GenerateContentResponse:
            return self.adapter.client.models.generate_content(
                model=model_id,
                contents=contents,
                config=cfg,
            )

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_invoke)
        try:
            resp = fut.result(timeout=self.transcribe_timeout_sec)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        return extract_transcript_text(resp)

    def load_ears(self) -> tuple[bool, str]:
        """Load the local faster-whisper model once at startup (no-op for BACKEND=gemini)."""
        if self.ears is None:
            return False, "Local ears are only used with BACKEND=local."
        try:
            self.ears.load()
        except EarsError as exc:
            logger.error("Local ears failed to load: %s", exc)
            return False, str(exc)
        return True, f"faster-whisper {self.config.whisper_model} ready (CPU int8)"

    def load_mouth(self) -> tuple[bool, str]:
        """Load the Piper voice once at startup (no-op for BACKEND=gemini)."""
        if self.mouth is None:
            return False, "Local mouth is only used with BACKEND=local."
        try:
            rate = self.mouth.sample_rate()
        except MouthError as exc:
            logger.error("Local mouth failed to load: %s", exc)
            return False, str(exc)
        return True, f"Piper {self.config.piper_voice} ready ({rate} Hz, CPU)"

    def _speak_local_piper(self, spoken: str) -> tuple[bool, str]:
        """Synthesize sentence by sentence and start playing each one while the rest generate."""
        mouth = self.mouth
        if mouth is None:  # pragma: no cover - guarded by speak_sync
            return False, "Local mouth is not available."

        sentences = split_sentences(spoken)
        if not sentences:
            return True, "Nothing to speak."

        jobs: queue.Queue[tuple[int, bytes] | None] = queue.Queue()
        stop = threading.Event()
        errors: list[str] = []

        def _producer() -> None:
            try:
                for index, sentence in enumerate(sentences):
                    if stop.is_set():
                        return
                    try:
                        wav_bytes = mouth.synthesize_wav(sentence)
                    except MouthError as exc:
                        errors.append(str(exc))
                        return
                    if stop.is_set():
                        return
                    jobs.put((index, wav_bytes))
            finally:
                jobs.put(None)

        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        # Show the speaking line before sound starts, not after the last sentence.
        self._notify_engine_once(f"Piper ({self.config.piper_voice})")
        worker = threading.Thread(target=_producer, name="piper-synth", daemon=True)
        worker.start()

        spoken_count = 0
        player = ""
        play_error = ""
        try:
            while True:
                item = jobs.get()
                if item is None:
                    break
                index, wav_bytes = item
                if stop.is_set():
                    break
                path = WORKSPACE_DIR / f"voice_output_{index}.wav"
                path.write_bytes(wav_bytes)
                ok_play, play_info = self._play_wav_file(path)
                if not ok_play:
                    play_error = play_info
                    stop.set()
                    break
                player = play_info
                spoken_count += 1
        finally:
            stop.set()
            worker.join(timeout=1.0)

        if errors:
            prefix = f"Piper spoke {spoken_count} sentence(s), then: " if spoken_count else ""
            return spoken_count > 0, f"{prefix}{errors[0]}"
        if play_error:
            prefix = f"Piper spoke {spoken_count} sentence(s), then: " if spoken_count else ""
            return spoken_count > 0, f"{prefix}{play_error}"
        if spoken_count == 0:
            return False, "Piper produced no audio."
        return True, f"Piper ({self.config.piper_voice}) via {player}"

    def transcribe_audio(self, wav_path: Path | None = None) -> tuple[bool, str]:
        """Transcribe a WAV: local faster-whisper when BACKEND=local, Gemini cloud otherwise."""
        if self.ears is not None:
            target = wav_path or self.audio_file
            ok, text = self.ears.transcribe(target)
            if ok:
                logger.info("Transcribed locally: %s", redact_secrets(text))
            confidence = dict(getattr(self.ears, "last_confidence", {}) or {})
            self._attach_debug_transcript(str(confidence.get("text") or (text if ok else "")))
            return ok, text
        return self._transcribe_cloud(wav_path)

    def _transcribe_cloud(self, wav_path: Path | None = None) -> tuple[bool, str]:
        """Transcribe WAV using transcribe -> fast -> main with 20s timeout and full error logging."""
        target = wav_path or self.audio_file
        if not target.exists():
            return False, "Audio file missing."
        if self.adapter is None:
            return False, "Cloud transcription needs a Gemini adapter (BACKEND=gemini)."

        audio_bytes = target.read_bytes()
        candidates = self._candidate_transcribe_models()
        if not candidates:
            return False, "No transcription models configured in .env."

        errors_seen: list[str] = []
        saw_no_speech = False

        for model_id in candidates:
            try:
                raw_text = self._call_transcribe_once(model_id, audio_bytes)
                cleaned = raw_text.strip()
                if cleaned.upper() == "[NO_SPEECH]" or "[NO_SPEECH]" in cleaned.upper():
                    logger.info("Model %s reported [NO_SPEECH].", model_id)
                    return False, "I didn't catch that"
                if not cleaned:
                    logger.warning("Model %s returned empty transcription text; trying fallback.", model_id)
                    saw_no_speech = True
                    continue
                logger.info("Transcribed via %s: %s", model_id, redact_secrets(cleaned))
                return True, cleaned
            except concurrent.futures.TimeoutError:
                err_msg = f"{model_id} timed out after {int(self.transcribe_timeout_sec)}s"
                logger.error("Transcription timeout: %s", err_msg)
                errors_seen.append(err_msg)
            except Exception as exc:
                safe_err = redact_secrets(f"{model_id}: {type(exc).__name__}: {exc}")
                logger.error("Transcription error on %s: %s", model_id, safe_err)
                errors_seen.append(safe_err)

        if saw_no_speech and not errors_seen:
            return False, "I didn't catch that"
        if errors_seen:
            return False, f"Transcription failed ({'; '.join(errors_seen)})"
        return False, "I didn't catch that"

    def _mark_cloud_tts_failure(self, err: str) -> None:
        """Pause cloud TTS attempts for a while so replied speech never stalls on a dead engine."""
        lowered = err.lower()
        if "resource_exhausted" in lowered or "429" in lowered:
            cooldown = 7200.0  # quota exhausted; recheck in ~2h (a fresh 429 is instant, not a stall)
        else:
            cooldown = 300.0
        self._cloud_tts_retry_after = time.monotonic() + cooldown
        logger.warning("Cloud TTS paused for %ds after failure.", int(cooldown))

    def synthesize_cloud_tts(self, spoken_text: str) -> tuple[bool, bytes, str]:
        """Call config.models['tts'] with response_modalities=['AUDIO'] and return normalized WAV bytes."""
        tts_model = (self.config.models.get("tts") or "").strip()
        if not tts_model:
            return False, b"", "No GEMINI_TTS_MODEL configured in .env."

        voice_name = (self.config.tts_voice or "Kore").strip() or "Kore"
        gen_cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            ),
            http_options=types.HttpOptions(timeout=20000),
        )

        def _invoke() -> types.GenerateContentResponse:
            return self.adapter.client.models.generate_content(
                model=tts_model,
                contents=spoken_text,
                config=gen_cfg,
            )

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_invoke)
        try:
            resp = fut.result(timeout=20.0)
        except concurrent.futures.TimeoutError:
            err = f"Gemini TTS ({tts_model}) timed out after 20s."
            logger.warning(err)
            self._mark_cloud_tts_failure(err)
            return False, b"", err
        except Exception as exc:
            err = redact_secrets(f"Gemini TTS ({tts_model}) error: {type(exc).__name__}: {exc}")
            logger.warning(err)
            self._mark_cloud_tts_failure(err)
            return False, b"", err
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        audio_bytes = b""
        for cand in getattr(resp, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    audio_bytes = inline.data
                    break
            if audio_bytes:
                break

        if not audio_bytes:
            err = f"Gemini TTS ({tts_model}) returned no audio bytes."
            logger.warning(err)
            self._mark_cloud_tts_failure(err)
            return False, b"", err

        try:
            wav_bytes = ensure_wav_bytes(
                audio_bytes,
                default_rate=24000,
                default_channels=1,
                normalize_to_minus_3dbfs=True,
                volume_gain=self.config.voice_volume,
            )
            self._cloud_tts_retry_after = 0.0
            return True, wav_bytes, f"Gemini TTS ({tts_model}/{voice_name})"
        except Exception as exc:
            err = f"Failed to decode/normalize TTS audio: {exc}"
            logger.warning(err)
            self._mark_cloud_tts_failure(err)
            return False, b"", err

    def _play_wav_file(self, wav_path: Path) -> tuple[bool, str]:
        """Play a WAV file using pw-play -> paplay -> aplay."""
        players: list[list[str]] = []
        if shutil.which("pw-play"):
            players.append(["pw-play", str(wav_path)])
        if shutil.which("paplay"):
            players.append(["paplay", str(wav_path)])
        if shutil.which("aplay"):
            players.append(["aplay", "-q", str(wav_path)])

        if not players:
            return False, "No audio player found (install pipewire-utils for pw-play)."

        for cmd in players:
            proc: subprocess.Popen[str] | None = None
            try:
                with self._play_lock:
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    self._play_proc = proc
                _, stderr_txt = proc.communicate(timeout=30.0)
                if proc.returncode in (0, -15, -9):
                    return True, cmd[0]
                logger.warning("Player %s exited with %s: %s", cmd[0], proc.returncode, stderr_txt.strip())
            except Exception as exc:
                logger.warning("Player %s failed: %s", cmd[0], exc)
            finally:
                with self._play_lock:
                    self._play_proc = None
                    try:
                        self._speech_procs.remove(proc)
                    except ValueError:
                        pass
                # Ctrl+C during playback must not leave pw-play talking to an empty room.
                _terminate_process(proc)
        return False, "All local WAV players (pw-play/paplay/aplay) failed."

    def _speak_fallback_local(self, spoken_text: str) -> tuple[bool, str]:
        """Fallback chain: spd-say -> espeak-ng."""
        vol_int = int(round(clamp_voice_volume(self.config.voice_volume) * 100))
        if shutil.which("spd-say"):
            spd_vol = max(-100, min(100, (vol_int - 100)))
            # Voice personality knobs (default: confident young male ~18yo).
            cmd = [
                "spd-say", "--wait", "-i", str(spd_vol),
                "--voice-type", self.config.tts_fallback_voice_type,
                "--pitch", str(self.config.tts_fallback_pitch),
                "--rate", str(self.config.tts_fallback_rate),
                spoken_text,
            ]
            proc: subprocess.Popen[str] | None = None
            try:
                with self._play_lock:
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    self._play_proc = proc
                    self._speech_procs.append(proc)
                proc.communicate(timeout=25.0)
                if proc.returncode in (0, -15, -9):
                    self._notify_engine_once("Fallback TTS: spd-say")
                    return True, "Fallback TTS: spd-say"
            except Exception as exc:
                logger.warning("spd-say failed: %s", exc)
            finally:
                with self._play_lock:
                    self._play_proc = None
                    try:
                        self._speech_procs.remove(proc)
                    except ValueError:
                        pass

        if shutil.which("espeak-ng"):
            amp = max(50, min(200, int(round(150 * clamp_voice_volume(self.config.voice_volume)))))
            cmd = ["espeak-ng", "-a", str(amp), "-s", "165", spoken_text]
            proc = None
            try:
                with self._play_lock:
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    self._play_proc = proc
                    self._speech_procs.append(proc)
                proc.communicate(timeout=25.0)
                if proc.returncode in (0, -15, -9):
                    self._notify_engine_once("Fallback TTS: espeak-ng")
                    return True, "Fallback TTS: espeak-ng"
            except Exception as exc:
                logger.warning("espeak-ng failed: %s", exc)
            finally:
                with self._play_lock:
                    self._play_proc = None
                    try:
                        self._speech_procs.remove(proc)
                    except ValueError:
                        pass

        return False, "All TTS engines (Gemini TTS, spd-say, espeak-ng) failed."

    def speak_sync(self, text: str) -> tuple[bool, str]:
        """Clean text, then speak via local Piper (local backend) or Gemini TTS (cloud backend)."""
        self.stop_speaking()
        # Local Piper uses the configured spoken-reply cap so a chatty small model still
        # speaks one short sentence.
        max_chars = self.config.voice_reply_max_chars if self.mouth is not None else 220
        spoken = clean_speech_text(text, max_chars=max_chars)
        if not spoken:
            return True, "Nothing to speak."

        if self.mouth is not None:
            return self._speak_local_piper(spoken)

        ok_tts, wav_bytes, info = False, b"", ""
        if self.adapter is None:
            logger.info("No Gemini adapter: using the system voice fallback.")
        elif time.monotonic() >= self._cloud_tts_retry_after:
            ok_tts, wav_bytes, info = self.synthesize_cloud_tts(spoken)
        else:
            logger.info("Skipping cloud TTS (cooldown active); using local fallback voice.")
        if ok_tts and wav_bytes:
            WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
            self.tts_file.write_bytes(wav_bytes)
            self._notify_engine_once(info)
            ok_play, play_info = self._play_wav_file(self.tts_file)
            if ok_play:
                return True, f"{info} via {play_info}"
            logger.warning("Cloud TTS WAV playback failed (%s); falling back to local TTS.", play_info)

        return self._speak_fallback_local(spoken)

    def speak(self, text: str, on_done: Callable[[bool, str], None] | None = None) -> threading.Thread:
        """Speak reply asynchronously in a background thread, killing any previous playback first."""
        self.stop_speaking()

        def _worker() -> None:
            ok, msg = self.speak_sync(text)
            if on_done is not None:
                on_done(ok, msg)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t
