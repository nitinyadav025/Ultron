"""Automatic microphone recording: listen until the user stops talking.

Why this module exists
----------------------
The old recorder always grabbed a fixed window (3 s in ``voice-test``, 5 s in the chat
loop) and stopped with SIGINT. Live tests showed sentences cut in half and the first
word lost: ``pw-record`` needs tens of milliseconds to open the device, and the window
ended while the user was still talking.

``record_until_silence()`` waits for speech to start, keeps ~300 ms of audio from before
the first word, stops after a real pause (a short pause in a sentence does not stop it),
has a hard cap, and reports "no speech" so pure silence is never sent to Whisper.

Detection is a plain energy detector with a measured noise floor. It was chosen over the
bundled Silero VAD because it needs no model load, runs on 20 ms frames in real time, and
can be tested deterministically with synthetic audio (see ``SyntheticSource``).

Every byte comes from an ``AudioSource``, so tests replay fake chunks with no microphone.
"""

from __future__ import annotations

import array
import math
import queue
import select
from collections import deque
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320 samples = 20 ms
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH

# The HDA mic emits a short full-scale thump (~250-350 ms) when its PipeWire node wakes
# from suspend. It is not the user, so it must never count as speech start. Every new
# pw-record stream also opens with a loud pop that decays over ~300-400 ms (measured in
# the saved live attempts), so the first RECORDER_WARMUP_MS of the stream is ignored
# completely: it is not used for the noise floor, start detection or the saved audio.
WARMUP_MS = 400
DEFAULT_WARMUP_MS = 300
DEFAULT_WARMUP_MAX_MS = 1500
# A 50 ms block at or below this level is "calm" after the opening pop. Measured: the
# three real pop takes stay above 1000 until about 700-800 ms; the fan's 100 ms RMS is
# 265-1025.
WARMUP_CALM_RMS = 1200.0
# Noise floor measured from the first frames (clipped frames excluded).
CALIBRATION_MS = 200
PRE_ROLL_MS = 400  # keep the first word safe
MIN_SPEECH_MS = 200  # clicks and bumps shorter than this are not speech

# Start detection: a low, capped threshold so a soft first word (about 5x quieter than
# the rest) still starts the recording, while a quiet room stays below it.
ABS_START_RMS = 280
MAX_START_RMS = 500
START_NOISE_MULTIPLIER = 2.5
DEFAULT_NOISE_FLOOR = 60.0
MIN_NOISE_FLOOR = 40.0
# A frame is speech only when it is also clearly above the *running* noise floor. Steady
# room noise (the fan) defines that floor, so it can never pass this test no matter how
# loud it is. 10 dB = x3.16.
MIN_SNR_DB = 10.0
# Perfectly steady loud sound is not a voice. Speech changes level between syllables;
# after 8 s without any change the capture is abandoned as noise.
STEADY_SPEECH_LIMIT_MS = 8000
STEADY_LEVEL_CV = 0.06

# End detection: hysteresis (clearly lower than the start threshold) and relative to the
# speaker's own level in this recording. The Nov 1 live log showed a steady fan at RMS
# 1700 kept resetting a 30 s capture while the speaker's own level was ~2400: 45 % of the
# speaker's level is 1080, so 1700 still looked like voice to an energy-only detector.
# Silero VAD is the second signal that ends that case (see VAD_* below).
ABS_END_RMS = 140
END_NOISE_MULTIPLIER = 1.8
END_SPEAKER_FRACTION = 0.18
# The end threshold must follow the *active speech* level, not just the room floor.
# A frame is silence when it is below max(end floor, 45 % of the speaker's active level),
# at least 60 % below it (<= 40 %), or within 6 dB of a recent post-speech floor.
SPEECH_RMS_FRACTION = 0.45
SPEECH_DROP_FRACTION = 0.40
POST_FLOOR_MULTIPLIER = 2.0  # +6 dB

# Silero VAD endpointing (B6). Once speech starts, the last 900 ms are checked every
# 150 ms; 0 ms of human speech in the last 850 ms ends the utterance even when a fan keeps
# the energy high. A 1.2 s VAD-silent run is the hard backstop.
VAD_WINDOW_MS = 900
VAD_EVAL_MS = 850
VAD_CHECK_MS = 150
VAD_BACKSTOP_MS = 1200
VAD_START_MS = 300
DEFAULT_MAX_SECONDS = 12.0
# A background louder than the first calibration is still background. The rolling floor
# follows it so a fan/room hum can never leave noise_floor stuck at 85.
NOISE_FLOOR_WINDOW_MS = 3000
# 10th percentile, not 20th: a short recording may have only 500 ms of room noise before
# speech, and 160 ms of speech is already 20 % of that window. The 10th percentile stays
# on the room noise until the background really dominates.
NOISE_FLOOR_PERCENTILE = 0.10
# Last-resort stop: 2.5 s after the last frame at >=45 % of the peak speech level, steady
# low audio (room/fan) ends the utterance instead of running to max_length.
POST_SPEECH_PEAK_FRACTION = 0.45
POST_SPEECH_CHECK_MS = 2500
RECENT_LEVEL_WINDOW = 25  # 500 ms of 20 ms frames
# A short command ("Ultron", "mute") must end as soon as the 900 ms silence is reached.
MIN_SPEECH_BEFORE_SILENCE_STOP_MS = 400

# A saturated burst (wake-up thump / stuck full-scale noise) is not a voice: its RMS is
# very close to its peak. Real speech, even clipped, keeps a crest factor above ~2.8.
CLIPPED_BURST_CREST = 2.2
CLIP_LEVEL = 31000  # a frame whose peak reaches this is (near) full scale

# Trailing silence kept after speech so the last word is not clipped.
TRAILING_SILENCE_MS = 300
LEVEL_TRACE_MS = 100  # debug trace resolution


class RecorderError(RuntimeError):
    """Raised when no working capture command is available."""


class AudioSource(Protocol):
    """Minimal streaming source: 16 kHz mono s16 chunks, one frame at a time."""

    def start(self) -> None:
        ...

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        """Return up to ``max_bytes`` of PCM, b'' on timeout, None when the source ended."""

    def stop(self) -> None:
        ...


# --------------------------------------------------------------------------------------
# PCM helpers (array based: no numpy dependency, Python 3.14 safe)
# --------------------------------------------------------------------------------------


def pcm16_samples(pcm: bytes) -> array.array:
    """Return the samples of 16-bit little-endian PCM as a native-endian int16 array."""
    usable = len(pcm) - (len(pcm) % 2)
    samples = array.array("h")
    if usable:
        samples.frombytes(pcm[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
    return samples


def samples_to_pcm16(samples: array.array) -> bytes:
    out = array.array("h", samples)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def frame_rms(samples: array.array) -> int:
    if not samples:
        return 0
    total = 0
    for value in samples:
        total += value * value
    return math.isqrt(total // len(samples))


def frame_peak(samples: array.array) -> int:
    peak = 0
    for value in samples:
        magnitude = -value if value < 0 else value
        if magnitude > peak:
            peak = magnitude
    return peak


def percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile for a non-empty list of ints."""
    if not values:
        return 0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * fraction))
    return ordered[max(0, min(len(ordered) - 1, index))]


def median(values: list[int]) -> int:
    return percentile(values, 0.5)


def split_frames(pcm: bytes) -> list[array.array]:
    samples = pcm16_samples(pcm)
    return [samples[i : i + FRAME_SAMPLES] for i in range(0, len(samples), FRAME_SAMPLES)]


# --------------------------------------------------------------------------------------
# Recording result + signature
# --------------------------------------------------------------------------------------


@dataclass
class Recording:
    """Result of one capture."""

    pcm: bytes = b""
    saw_speech: bool = False
    reason: str = "error"  # silence | timeout | max_length | enter | cancelled | source_ended | no_audio | fixed
    duration_ms: int = 0
    speech_ms: int = 0
    debug_pcm: bytes = b""  # raw audio kept even when no speech was confirmed
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.pcm) and self.saw_speech


# --------------------------------------------------------------------------------------
# Real source: pw-record (PipeWire) or arecord (ALSA) streaming raw PCM on stdout
# --------------------------------------------------------------------------------------


class PwRecordSource:
    """Streams 16 kHz mono s16 PCM from ``pw-record`` (or ``arecord``) over a pipe."""

    def __init__(self, rate: int = SAMPLE_RATE, target: str = "") -> None:
        self.rate = rate
        self.target = (target or "").strip()
        self.error = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._cmd: list[str] = []

    @staticmethod
    def command(rate: int = SAMPLE_RATE, target: str = "") -> list[str]:
        if shutil.which("pw-record"):
            cmd = [
                "pw-record",
                "--raw",
                "--format=s16",
                f"--rate={rate}",
                "--channels=1",
            ]
            if target:
                cmd += ["--target", target]
            return cmd + ["-"]
        if shutil.which("arecord"):
            return ["arecord", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw"]
        raise RecorderError("Neither pw-record (pipewire-utils) nor arecord (alsa-utils) is installed.")

    def start(self) -> None:
        self._cmd = self.command(self.rate, self.target)
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise RecorderError(f"Microphone recorder failed to start: {type(exc).__name__}: {exc}") from exc
        self._reader = threading.Thread(target=self._pump, name="mic-reader", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._queue.put(None)
            return
        stdout = proc.stdout
        try:
            while True:
                data = stdout.read(FRAME_BYTES)
                if not data:
                    break
                self._queue.put(data)
        except Exception:  # pragma: no cover - pipe torn down at shutdown
            pass
        finally:
            self._queue.put(None)

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return b""
        return item

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        # SIGINT lets pw-record close the capture stream cleanly (SIGTERM can drop a block).
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
        if proc.stderr is not None:
            try:
                self.error = proc.stderr.read().decode("utf-8", "replace").strip()
            except Exception:
                self.error = ""
            finally:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=0.5)


class SyntheticSource:
    """Test source: replays prepared PCM, one frame per ``read`` call, then ends.

    ``chunks`` are complete frames (bytes). A callback may be supplied per frame so a
    test can assert on the recorder's behaviour while it runs.
    """

    def __init__(self, pcm: bytes | list[bytes], on_read: Callable[[int], None] | None = None) -> None:
        if isinstance(pcm, (bytes, bytearray)):
            data = bytes(pcm)
            self._chunks: list[bytes] = [data[i : i + FRAME_BYTES] for i in range(0, len(data), FRAME_BYTES)]
        else:
            self._chunks = list(pcm)
        self._index = 0
        self._on_read = on_read
        self.error = ""
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        if self._index >= len(self._chunks):
            return None
        chunk = self._chunks[self._index]
        self._index += 1
        if self._on_read is not None:
            self._on_read(self._index)
        return chunk

    def stop(self) -> None:
        self.stopped = True


# --------------------------------------------------------------------------------------
# "Listen until the user stops talking"
# --------------------------------------------------------------------------------------


def _is_clipped_burst(level: int, peak: int) -> bool:
    """A saturated burst (wake-up thump) is not a voice: RMS sits very close to the peak."""
    return peak >= CLIP_LEVEL and level * CLIPPED_BURST_CREST > peak


class AdaptiveWarmup:
    """Discard the stream-opening pop until the level is calm, bounded by min/max time.

    The pop is never used for the noise floor, start detection or saved audio. It ends
    after two consecutive calm 50 ms blocks (at most ``max_ms``).
    """

    def __init__(
        self,
        min_ms: int = DEFAULT_WARMUP_MS,
        max_ms: int = DEFAULT_WARMUP_MAX_MS,
        calm_rms: float = WARMUP_CALM_RMS,
    ) -> None:
        self.min_ms = int(min_ms)
        self.max_ms = max(self.min_ms, int(max_ms))
        self.calm_rms = float(calm_rms)
        self.done = False
        self.end_ms = self.min_ms
        self._block_start = 0
        self._sum = 0
        self._count = 0
        self._prev: float | None = None
        self._calm_run = 0
        self._burst = False

    def feed(self, level: int, peak: int, audio_ms: int) -> bool:
        """Return True while frames must still be discarded."""
        if self.done:
            return False
        burst = _is_clipped_burst(level, peak)
        if burst:
            self._burst = True
        self._sum += level
        self._count += 1
        if audio_ms - self._block_start >= 50:
            block = self._sum / max(1, self._count)
            self._sum = 0
            self._count = 0
            self._block_start = audio_ms
            if not self._burst:
                calm = block <= self.calm_rms and (self._prev is None or block <= self._prev * 1.2)
                self._calm_run = self._calm_run + 1 if calm else 0
                self._prev = block
                if audio_ms >= self.min_ms and self._calm_run >= 2:
                    self.end_ms = audio_ms
                    self.done = True
                    return False
            self._burst = False
        if audio_ms >= self.max_ms:
            self.end_ms = self.max_ms
            self.done = True
            return False
        return True


def _snr_ratio(min_snr_db: float) -> float:
    return 10.0 ** (float(min_snr_db) / 20.0)


class RollingNoiseFloor:
    """Track the quiet background even when it is louder than the first calibration.

    The old floor only adapted while ``level <= floor * 1.5``. With an initial floor of 85
    and a fan at 1400 that condition never held, so ``noise_floor`` stayed at 85 all
    session while the fan made every utterance run to the 30 s cap. This class keeps the
    last 3 s of non-speech frames and follows their 20th percentile. Speech is never fed
    into it (the caller skips frames VAD/energy calls speech), so a loud word cannot raise
    the floor.
    """

    def __init__(
        self,
        initial: float = DEFAULT_NOISE_FLOOR,
        *,
        window_ms: int = NOISE_FLOOR_WINDOW_MS,
        percentile_value: float = NOISE_FLOOR_PERCENTILE,
    ) -> None:
        self.floor = float(max(MIN_NOISE_FLOOR, initial))
        self.percentile = float(percentile_value)
        self._levels: deque[int] = deque(maxlen=max(5, int(window_ms) // FRAME_MS))

    def set(self, value: float) -> None:
        self.floor = float(max(MIN_NOISE_FLOOR, value))
        self._levels.clear()

    def feed(self, level: int) -> None:
        """Add one non-speech frame and move the floor toward its low percentile."""
        if level <= 0:
            return
        self._levels.append(int(level))
        if len(self._levels) < 5:
            return
        estimate = float(percentile(list(self._levels), self.percentile))
        if estimate > self.floor * 1.5:
            # A louder steady background must be tracked quickly, not ignored.
            self.floor = 0.70 * self.floor + 0.30 * estimate
        else:
            self.floor = 0.95 * self.floor + 0.05 * estimate
        self.floor = max(MIN_NOISE_FLOOR, self.floor)


def _start_threshold(noise_floor: float, min_snr_db: float = MIN_SNR_DB) -> float:
    return max(
        float(ABS_START_RMS),
        min(noise_floor * START_NOISE_MULTIPLIER, float(MAX_START_RMS)),
        noise_floor * _snr_ratio(min_snr_db),
    )


def _end_threshold(noise_floor: float, start_threshold: float, speaker_level: float) -> float:
    noise_term = noise_floor * END_NOISE_MULTIPLIER
    if speaker_level > 0:
        # A fan just below the voice must not make the end threshold *above* the voice:
        # with floor 1700 and speech 2400 the old 1.8x term was 3060 and cut mid-word.
        noise_term = min(noise_term, speaker_level * 0.5)
    return max(
        float(ABS_END_RMS),
        noise_term,
        min(start_threshold * 0.6, max(1.0, speaker_level) * END_SPEAKER_FRACTION),
    )


def speech_rms_percentile(levels: list[int] | deque[int]) -> float:
    """The speaker's active level: 75th percentile of the speech-like frames so far."""
    values = list(levels)
    return float(percentile(values, 0.75)) if values else 0.0


def is_end_silence(
    level: int,
    end_threshold: float,
    speech_rms: float,
    post_floor: float = 0.0,
) -> bool:
    """True when this frame is post-speech quiet, relative to the speaker's own level.

    ``post_floor`` is the recent background estimate. A level within 6 dB of it is quiet
    even when the fan is louder than 45 % of the speaker: that is the case the Nov 2 log
    showed (fan RMS 1700, speech RMS 2400).
    """
    if speech_rms > 0:
        if post_floor > 0 and level <= post_floor * POST_FLOOR_MULTIPLIER:
            # A level near the recent post-speech floor is background, but never call a
            # frame that is still near the speaker's own level silent. Without this guard
            # a fan at 1700 would mark the 2400 speech as quiet (the early-cut replay).
            if level < 0.8 * speech_rms:
                return True
        if level < max(float(end_threshold), SPEECH_RMS_FRACTION * speech_rms):
            return True
        # A >=60 % drop from the active speech level is silence too, even when the room
        # noise happens to sit slightly above the absolute end threshold.
        return level <= SPEECH_DROP_FRACTION * speech_rms
    if post_floor > 0 and level <= post_floor * POST_FLOOR_MULTIPLIER:
        return True
    return level < float(end_threshold)


def is_steady_post_speech(
    levels: list[int] | deque[int], peak_speech_rms: float, quiet_ms: float
) -> bool:
    """Backstop for a room noise that never drops below the fractional end threshold.

    After ``POST_SPEECH_CHECK_MS`` without any frame at >=45 % of the peak speech level,
    steady low audio is room/fan noise, not a continuing sentence.
    """
    if quiet_ms < POST_SPEECH_CHECK_MS or peak_speech_rms <= 0:
        return False
    values = list(levels)[-RECENT_LEVEL_WINDOW:]
    if len(values) < RECENT_LEVEL_WINDOW // 2:
        return False
    limit = POST_SPEECH_PEAK_FRACTION * peak_speech_rms
    if any(value >= limit for value in values):
        return False
    mean = sum(values) / len(values)
    if mean <= 0:
        return True
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance) / mean < 0.35


def _default_enter_pressed() -> bool:
    """True when the user pressed Enter on a real terminal (newline is consumed)."""
    stream = sys.stdin
    if stream is None:
        return False
    try:
        if not stream.isatty():
            return False
        ready, _, _ = select.select([stream], [], [], 0)
        if not ready:
            return False
        stream.readline()  # consume it so the chat prompt does not receive a stray Enter
        return True
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def record_until_silence(
    source: AudioSource,
    *,
    start_timeout_s: float = 10.0,
    silence_ms: int = 850,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    min_speech_ms: int = MIN_SPEECH_MS,
    pre_roll_ms: int = PRE_ROLL_MS,
    status: Callable[[str], None] | None = None,
    stop_on_enter: bool = True,
    enter_pressed: Callable[[], bool] | None = None,
    warmup_ms: int = DEFAULT_WARMUP_MS,
    warmup_max_ms: int = DEFAULT_WARMUP_MAX_MS,
    min_snr_db: float = MIN_SNR_DB,
    vad: Any | None = None,
    vad_backstop_ms: int = VAD_BACKSTOP_MS,
) -> Recording:
    """Capture speech until 850 ms of continuous quiet after it started.

    The first ``warmup_ms`` of a new stream is discarded (the mic/pw-record opening pop).
    Start detection needs a level that is both above the low capped threshold and at least
    ``min_snr_db`` above the running noise floor. ``vad`` (Silero) is the second signal:
    when it is available it confirms the start and ends a fan that the energy rule would
    mistake for speech. End detection uses hysteresis plus the trailing 850 ms VAD check,
    so a short pause never cuts the sentence. Timing is measured in *audio* time, so
    synthetic tests run at full speed.

    ``stop_on_enter`` lets a real terminal finish the capture immediately; Enter before
    any speech cancels it. The newline is consumed, never left for the chat prompt.
    """
    notify = status or (lambda _msg: None)
    warm = AdaptiveWarmup(warmup_ms, warmup_max_ms)
    warmup_end_ms = int(warmup_ms)

    pre_roll_frames = max(1, (pre_roll_ms + min_speech_ms) // FRAME_MS)
    pre_roll: list[array.array] = []
    collected: list[array.array] = []
    speech_levels: list[int] = []
    active_speech_levels: list[int] = []
    recent_levels: deque[int] = deque(maxlen=RECENT_LEVEL_WINDOW)
    vad_frames: deque[array.array] = deque(maxlen=max(1, VAD_WINDOW_MS // FRAME_MS))
    peak_speech_rms = 0.0
    loud_until_ms = 0
    calibration: list[int] = []
    trace_levels: list[dict[str, Any]] = []
    bucket: list[tuple[int, int, bool]] = []

    floor = RollingNoiseFloor()
    noise_floor = floor.floor
    calibrated = False
    in_speech = False
    constant_noise = False
    steady_checked = False
    speech_activity = 0.0
    energy_quiet_run = 0
    vad_quiet_run = 0
    vad_start_run = 0
    last_vad_ms = 0
    last_vad_speech_ms = 0
    vad_usable = vad is not None
    speech_frames = 0
    audio_ms = 0
    speech_start_ms = 0
    speaker_level = 0.0
    start_threshold = _start_threshold(noise_floor, min_snr_db)
    end_threshold = _end_threshold(noise_floor, start_threshold, speaker_level)
    empty_reads = 0
    carry = b""
    reason = "no_audio"
    started = time.monotonic()
    poll_enter = enter_pressed or (_default_enter_pressed if stop_on_enter else (lambda: False))

    def _vad_check(elapsed_ms: int) -> None:
        nonlocal last_vad_speech_ms, vad_quiet_run, vad_start_run, vad_usable
        if vad is None or not vad_usable:
            return
        result = vad.speech_ms_in_window(
            list(vad_frames), window_ms=VAD_EVAL_MS, context_ms=VAD_WINDOW_MS
        )
        if result is None:
            vad_usable = False
            vad_quiet_run = 0
            vad_start_run = 0
            return
        last_vad_speech_ms = int(result)
        if in_speech:
            if last_vad_speech_ms <= 0:
                vad_quiet_run += max(1, int(elapsed_ms))
            else:
                vad_quiet_run = 0
        elif last_vad_speech_ms >= 150:
            vad_start_run += max(1, int(elapsed_ms))
        else:
            vad_start_run = 0

    def _start_speech() -> None:
        nonlocal in_speech, speech_start_ms, speaker_level, active_speech_levels
        nonlocal peak_speech_rms, loud_until_ms
        in_speech = True
        speech_start_ms = max(0, audio_ms - min_speech_ms)
        recent = pre_roll[-max(1, min_speech_ms // FRAME_MS):]
        speaker_level = float(max((frame_rms(item) for item in recent), default=0))
        active_speech_levels = [frame_rms(item) for item in recent if frame_rms(item) >= ABS_START_RMS]
        recent_levels.clear()
        for recent_level in active_speech_levels[-RECENT_LEVEL_WINDOW:]:
            recent_levels.append(recent_level)
        if active_speech_levels:
            # Robust peak (p90), not one loud transient: a single very loud frame must not
            # make the rest of the sentence look quiet.
            peak_speech_rms = float(percentile(active_speech_levels, 0.90))
            loud_until_ms = audio_ms
        notify("Heard you...")
        collected.extend(pre_roll)
        pre_roll.clear()

    try:
        source.start()
        while True:
            if stop_on_enter and poll_enter():
                reason = "enter" if in_speech else "cancelled"
                break

            chunk = source.read(FRAME_BYTES, timeout=0.25)
            if chunk is None:
                reason = "source_ended" if in_speech else "no_audio"
                break
            if not chunk:
                empty_reads += 1
                if empty_reads * 250 >= 5000 or time.monotonic() - started > max_seconds + 10:
                    reason = "no_audio"
                    break
                continue
            empty_reads = 0

            carry += chunk
            while len(carry) >= FRAME_BYTES:
                raw_frame, carry = carry[:FRAME_BYTES], carry[FRAME_BYTES:]
                frame = pcm16_samples(raw_frame)
                audio_ms += FRAME_MS
                level = frame_rms(frame)
                peak = frame_peak(frame)
                burst = _is_clipped_burst(level, peak)

                # Never calibrate or record the stream-opening pop.
                if not warm.done:
                    still_warm = warm.feed(level, peak, audio_ms)
                    if still_warm:
                        continue
                    warmup_end_ms = warm.end_ms
                    calibrated = False
                    calibration.clear()
                    notify(
                        "Listening... speak now (press Enter when done)"
                        if stop_on_enter
                        else "Listening... speak now"
                    )
                    continue  # the frame that ended the warm-up is discarded too

                vad_frames.append(frame)

                # --- noise floor: 25th percentile of the first 200 ms, bursts ignored ---
                if not calibrated:
                    if not burst:
                        calibration.append(level)  # digital zeros count as a very quiet room
                    if audio_ms >= warmup_end_ms + CALIBRATION_MS:
                        estimate = float(percentile(calibration, 0.25)) if calibration else DEFAULT_NOISE_FLOOR
                        floor.set(max(MIN_NOISE_FLOOR, estimate))
                        noise_floor = floor.floor
                        calibrated = True

                noise_floor = floor.floor
                start_threshold = _start_threshold(noise_floor, min_snr_db)
                end_threshold = _end_threshold(noise_floor, start_threshold, speaker_level)
                is_speech = level >= start_threshold and not burst

                if (
                    vad is not None
                    and vad_usable
                    and calibrated
                    and audio_ms - last_vad_ms >= VAD_CHECK_MS
                ):
                    elapsed_vad = audio_ms - last_vad_ms
                    last_vad_ms = audio_ms
                    _vad_check(elapsed_vad)

                if not in_speech:
                    pre_roll.append(frame)
                    if len(pre_roll) > pre_roll_frames:
                        pre_roll.pop(0)
                    if is_speech:
                        # Speech has syllable gaps; accumulate net speech instead of
                        # demanding an unbroken run. A fan's short gusts decay away.
                        speech_activity = min(1.0, speech_activity + FRAME_MS / max(1, min_speech_ms))
                        speech_frames += 1
                    else:
                        speech_activity = max(0.0, speech_activity - FRAME_MS / max(1, 2 * min_speech_ms))
                    # Follow a background that is louder than the first calibration. Never
                    # feed a frame that is speech into the floor, or the gate would rise
                    # above the user's own voice. VAD is the judge when it is available.
                    if vad is None or not vad_usable:
                        feed_floor = not is_speech
                    else:
                        feed_floor = last_vad_ms > 0 and last_vad_speech_ms <= 0
                    if (
                        calibrated
                        and audio_ms > warmup_end_ms
                        and not burst
                        and feed_floor
                    ):
                        floor.feed(level)
                        noise_floor = floor.floor
                    frame_active = is_speech
                    start_now = (
                        vad is not None
                        and vad_usable
                        and vad_start_run >= VAD_START_MS
                    ) or (
                        (vad is None or not vad_usable)
                        and speech_activity >= 1.0
                    )
                    if start_now:
                        _start_speech()
                    elif audio_ms >= int(start_timeout_s * 1000):
                        reason = "timeout"
                        break
                else:
                    collected.append(frame)
                    speech_levels.append(level)
                    recent_levels.append(level)
                    current_speech_rms = speech_rms_percentile(active_speech_levels)
                    # Room noise can sit above the low start threshold. Once speech has
                    # started, a frame that is within 6 dB of the floor or far below the
                    # speaker's level is not part of the sentence.
                    frame_active = not is_end_silence(
                        level, end_threshold, current_speech_rms, noise_floor
                    )
                    speech_like = level >= start_threshold and not burst and frame_active
                    if speech_like:
                        speaker_level = max(speaker_level, float(level))
                        speech_frames += 1
                        active_speech_levels.append(level)
                        if len(active_speech_levels) > 250:
                            active_speech_levels.pop(0)
                        peak_speech_rms = max(
                            peak_speech_rms, float(percentile(active_speech_levels, 0.90))
                        )
                        if level >= POST_SPEECH_PEAK_FRACTION * peak_speech_rms:
                            loud_until_ms = audio_ms
                    if is_end_silence(level, end_threshold, current_speech_rms, noise_floor):
                        energy_quiet_run += FRAME_MS
                    else:
                        energy_quiet_run = 0
                    quiet_run = max(energy_quiet_run, vad_quiet_run)
                    if (
                        quiet_run >= silence_ms
                        and audio_ms - speech_start_ms >= MIN_SPEECH_BEFORE_SILENCE_STOP_MS
                    ):
                        reason = "silence"
                        break
                    if (
                        vad is not None
                        and vad_usable
                        and vad_quiet_run >= vad_backstop_ms
                        and audio_ms - speech_start_ms >= MIN_SPEECH_BEFORE_SILENCE_STOP_MS
                    ):
                        # Silero heard no human speech for 1.2 s: a fan, not a sentence.
                        reason = "silence"
                        break
                    if is_steady_post_speech(
                        recent_levels, peak_speech_rms, audio_ms - loud_until_ms
                    ):
                        # Steady room/fan noise after the last speech burst: stop now
                        # instead of recording until the cap.
                        reason = "silence"
                        break
                    if audio_ms >= int(max_seconds * 1000):
                        reason = "max_length"
                        break
                    if not steady_checked and audio_ms - speech_start_ms >= STEADY_SPEECH_LIMIT_MS:
                        steady_checked = True
                        mean = sum(speech_levels) / len(speech_levels)
                        if mean > 0:
                            variance = sum((item - mean) ** 2 for item in speech_levels) / len(speech_levels)
                            if math.sqrt(variance) / mean < STEADY_LEVEL_CV:
                                # A perfectly steady level is a fan/engine, not a voice.
                                constant_noise = True
                                reason = "constant_noise"
                                break

                bucket.append((level, peak, frame_active))
                if audio_ms - (trace_levels[-1]["t_ms"] if trace_levels else 0) >= LEVEL_TRACE_MS:
                    trace_levels.append(
                        {
                            "t_ms": audio_ms,
                            "rms": int(sum(item[0] for item in bucket) / len(bucket)),
                            "peak": max(item[1] for item in bucket),
                            "speech": any(item[2] for item in bucket),
                            "end_thr": int(end_threshold),
                        }
                    )
                    bucket = []

            if reason in ("timeout", "silence", "max_length", "enter", "cancelled"):
                break
    finally:
        source.stop()

    if bucket:
        trace_levels.append(
            {
                "t_ms": audio_ms,
                "rms": int(sum(item[0] for item in bucket) / len(bucket)),
                "peak": max(item[1] for item in bucket),
                "speech": any(item[2] for item in bucket),
                "end_thr": int(end_threshold),
            }
        )

    trace: dict[str, Any] = {
        "mode": "auto",
        "thresholds": {
            "noise_floor": round(noise_floor, 1),
            "start": int(start_threshold),
            "end": int(end_threshold),
            "min_snr_db": round(float(min_snr_db), 1),
            "speech_rms": int(speech_rms_percentile(active_speech_levels)),
            "speech_rms_fraction": SPEECH_RMS_FRACTION,
            "post_speech_check_ms": POST_SPEECH_CHECK_MS,
            "warmup_ms": int(warmup_end_ms),
            "warmup_max_ms": int(warmup_max_ms),
            "silence_ms": silence_ms,
            "max_seconds": max_seconds,
            "vad": bool(vad is not None and vad_usable),
            "vad_quiet_ms": vad_quiet_run,
            "vad_backstop_ms": vad_backstop_ms,
            "start_timeout_s": start_timeout_s,
            "pre_roll_ms": pre_roll_ms,
            "min_speech_before_stop_ms": MIN_SPEECH_BEFORE_SILENCE_STOP_MS,
        },
        "speech_start_ms": speech_start_ms,
        "levels": trace_levels,
    }

    if not in_speech or constant_noise:
        debug_pcm = b"" if constant_noise else b"".join(samples_to_pcm16(frame) for frame in pre_roll)
        return Recording(
            pcm=b"",
            saw_speech=False,
            reason=reason,
            duration_ms=audio_ms,
            speech_ms=speech_frames * FRAME_MS,
            debug_pcm=debug_pcm,
            trace=trace,
        )

    if reason == "silence":
        # Cut the pause using the same speech-relative threshold: room noise that is
        # louder than the absolute end floor must not be kept as if it were speech.
        final_speech_rms = speech_rms_percentile(active_speech_levels)
        trim_threshold = max(end_threshold, SPEECH_RMS_FRACTION * final_speech_rms)
        last_speech = 0
        for index, frame in enumerate(collected):
            if frame_rms(frame) >= trim_threshold:
                last_speech = index
        keep_tail = TRAILING_SILENCE_MS // FRAME_MS
        collected = collected[: last_speech + 1 + keep_tail]

    pcm = b"".join(samples_to_pcm16(frame) for frame in collected)
    notify("Got it.")
    return Recording(
        pcm=pcm,
        saw_speech=True,
        reason=reason,
        duration_ms=audio_ms,
        speech_ms=speech_frames * FRAME_MS,
        debug_pcm=pcm,
        trace=trace,
    )


def record_for_duration(
    source: AudioSource,
    seconds: float,
    *,
    status: Callable[[str], None] | None = None,
    warmup_ms: int = DEFAULT_WARMUP_MS,
    warmup_max_ms: int = DEFAULT_WARMUP_MAX_MS,
) -> Recording:
    """Fixed-window capture over the same streaming source used by auto mode.

    This replaces ``pw-record <file>`` + SIGINT, which could end up with only the first
    word in the WAV. It records exactly ``seconds`` of audio (or until the source ends).
    """
    notify = status or (lambda _msg: None)
    warm = AdaptiveWarmup(warmup_ms, warmup_max_ms)
    target_ms = max(FRAME_MS, int(seconds * 1000))
    frames: list[array.array] = []
    carry = b""
    audio_ms = 0
    kept_ms = 0
    empty_reads = 0
    started = time.monotonic()

    try:
        source.start()
        while kept_ms < target_ms:
            chunk = source.read(FRAME_BYTES, timeout=0.25)
            if chunk is None:
                break
            if not chunk:
                empty_reads += 1
                if empty_reads * 250 >= 5000 or time.monotonic() - started > seconds + 10:
                    break
                continue
            empty_reads = 0
            carry += chunk
            while len(carry) >= FRAME_BYTES and kept_ms < target_ms:
                raw_frame, carry = carry[:FRAME_BYTES], carry[FRAME_BYTES:]
                frame = pcm16_samples(raw_frame)
                audio_ms += FRAME_MS
                if not warm.done:
                    if not warm.feed(frame_rms(frame), frame_peak(frame), audio_ms):
                        notify(f"Recording {seconds:.1f}s...")
                    continue
                frames.append(frame)
                kept_ms += FRAME_MS
    finally:
        source.stop()

    pcm = b"".join(samples_to_pcm16(frame) for frame in frames)
    notify("Got it.")
    levels = [
        {"t_ms": (index + 1) * FRAME_MS, "rms": frame_rms(frame), "peak": frame_peak(frame)}
        for index, frame in enumerate(frames)
        if (index + 1) % (LEVEL_TRACE_MS // FRAME_MS) == 0
    ]
    return Recording(
        pcm=pcm,
        saw_speech=has_speech_energy(pcm),
        reason="fixed",
        duration_ms=kept_ms,
        debug_pcm=pcm,
        trace={"mode": "fixed", "target_ms": target_ms, "levels": levels},
    )


def has_speech_energy(
    pcm: bytes,
    *,
    min_speech_ms: int = MIN_SPEECH_MS,
    start_skip_ms: int = 0,
) -> bool:
    """Offline check used by fixed-window mode: does this buffer contain real speech?

    A clipped wake-up pop is excluded frame by frame (burst rule), so no fixed lead-in is
    needed. The 10th-percentile floor ignores speech itself.
    """
    frames = split_frames(pcm)
    skip = max(0, start_skip_ms // FRAME_MS)
    frames = frames[skip:]
    if not frames:
        return False
    levels = [frame_rms(frame) for frame in frames]
    # The 10th percentile ignores speech itself, so a window that is mostly voice still
    # gets a low floor (a short fixed window can contain more speech than silence).
    floor = max(MIN_NOISE_FLOOR, float(percentile(levels, 0.10) or DEFAULT_NOISE_FLOOR))
    threshold = _start_threshold(floor)
    run = 0
    for index, level in enumerate(levels):
        burst = _is_clipped_burst(level, frame_peak(frames[index]))
        if level >= threshold and not burst:
            run += 1
            if run * FRAME_MS >= min_speech_ms:
                return True
        else:
            run = 0
    return False


# --------------------------------------------------------------------------------------
# Level repair: drop the wake-up thump, normalise on a robust level
# --------------------------------------------------------------------------------------


def trim_leading_transient(pcm: bytes, *, max_ms: int = 700) -> tuple[bytes, int]:
    """Drop the clipped burst at the very start of a capture (mic wake-up pop).

    Only a *leading* run of hard-clipped frames is removed, and only inside the first
    ``max_ms``. Genuine speech is never touched: trimming requires the first frame to be
    at (almost) full scale, which a mic at 27% volume does not produce from a voice.
    Returns ``(pcm_without_pop, dropped_ms)``.
    """
    frames = split_frames(pcm)
    if len(frames) < 2:
        return pcm, 0
    limit = min(len(frames), max(1, max_ms // FRAME_MS))
    levels = [frame_rms(frame) for frame in frames]

    # Compare the leading run with what comes *after* it. A very short window used to
    # give rest_median 0 and then drop every loud frame, including the speech.
    first_clean = 0
    while first_clean < limit and frame_peak(frames[first_clean]) >= CLIP_LEVEL:
        first_clean += 1
    rest_levels = levels[first_clean:] or levels
    rest_median = median(rest_levels) or MIN_NOISE_FLOOR

    dropped = 0
    for frame in frames[:limit]:
        peak = frame_peak(frame)
        if peak >= CLIP_LEVEL or frame_rms(frame) > 20 * rest_median:
            dropped += 1
        else:
            break

    if dropped == 0 or frame_peak(frames[0]) < CLIP_LEVEL:
        return pcm, 0
    return pcm[dropped * FRAME_BYTES :], dropped * FRAME_MS


TARGET_SPEECH_RMS = int(32767 * 0.12)  # healthy speech level for Whisper (~-18 dBFS)
MAX_BOOST = 3.0
# Hard output ceiling: never let normalisation push the recording above ~0.9 full scale.
SAFE_OUTPUT_RATIO = 0.9
SAFE_OUTPUT_PEAK = int(32767 * SAFE_OUTPUT_RATIO)  # 29490
CLIPPING_SAMPLE_RATIO = 0.005  # >0.5% of samples at full scale = the mic is clipping


@dataclass
class LevelInfo:
    before_peak: int = 0
    before_rms: int = 0
    after_peak: int = 0
    after_rms: int = 0
    gain: float = 1.0
    speech_level: int = 0
    clipping: bool = False
    clipping_samples: int = 0
    notes: list[str] = field(default_factory=list)


def measure_peak_rms(pcm: bytes) -> tuple[int, int]:
    samples = pcm16_samples(pcm)
    if not samples:
        return 0, 0
    return frame_peak(samples), frame_rms(samples)


def normalise_mic_level(
    pcm: bytes,
    *,
    has_speech: bool = True,
    target_rms: int = TARGET_SPEECH_RMS,
    max_boost: float = MAX_BOOST,
) -> tuple[bytes, LevelInfo]:
    """Even out the recording on a *robust* level, and never let the output clip.

    The level is the 75th percentile of the 20 ms frame RMS values, so a few bad frames
    cannot set the gain. The gain is then capped by the TRUE raw peak (not a percentile):
    the result can never exceed ~0.9 full scale. A non-clipping recording is never turned
    down; only input already above 0.9 full scale is scaled down. Quiet speech is boosted
    at most 3x, and audio with no speech is never boosted.
    """
    samples = pcm16_samples(pcm)
    info = LevelInfo()
    if not samples:
        return pcm, info

    info.before_peak = frame_peak(samples)
    info.before_rms = frame_rms(samples)
    clipped = sum(1 for value in samples if value >= 32700 or value <= -32700)
    info.clipping_samples = clipped
    info.clipping = clipped > max(20, int(len(samples) * CLIPPING_SAMPLE_RATIO))

    levels = [frame_rms(samples[i : i + FRAME_SAMPLES]) for i in range(0, len(samples), FRAME_SAMPLES)]
    info.speech_level = percentile(levels, 0.75)

    if info.speech_level <= 0 or info.before_peak <= 0:
        info.after_peak, info.after_rms = info.before_peak, info.before_rms
        return pcm, info

    gain = float(target_rms) / float(info.speech_level)
    if not has_speech:
        gain = min(gain, 1.0)  # never amplify room noise
        info.notes.append("no speech in buffer; not boosting")
    gain = min(gain, max_boost)

    # Hard ceiling from the real maximum sample. The old code used the 95th-percentile
    # frame peak, so one loud frame could still be pushed to 32768 and clip.
    peak_cap = float(SAFE_OUTPUT_PEAK) / float(info.before_peak)
    gain = min(gain, peak_cap)
    if info.before_peak <= SAFE_OUTPUT_PEAK:
        gain = max(gain, 1.0)  # never attenuate a recording that is not clipping
    else:
        info.notes.append("input above 0.9 full scale; scaled down to 0.9")

    if abs(gain - 1.0) < 0.02 and info.before_peak <= SAFE_OUTPUT_PEAK:
        info.gain = 1.0
        info.after_peak, info.after_rms = info.before_peak, info.before_rms
        return pcm, info

    scaled = array.array(
        "h",
        (max(-32768, min(32767, int(round(value * gain)))) for value in samples),
    )
    out = samples_to_pcm16(scaled)
    info.gain = gain
    info.after_peak, info.after_rms = measure_peak_rms(out)
    return out, info


def debug_level_summary(pcm: bytes, *, window_ms: int = 100) -> list[str]:
    """Per-100 ms peak/rms lines plus where full-scale samples sit (VOICE_DEBUG=1)."""
    samples = pcm16_samples(pcm)
    if not samples:
        return ["(no samples)"]
    window = max(1, SAMPLE_RATE * window_ms // 1000)
    lines = [f"raw samples={len(samples)} ({len(samples) / SAMPLE_RATE:.2f}s)"]
    peak, rms = measure_peak_rms(pcm)
    full = [i for i, value in enumerate(samples) if value >= 32700 or value <= -32700]
    lines.append(f"raw peak={peak} rms={rms} full_scale_samples={len(full)}")
    if full:
        lines.append(
            f"  full-scale positions: first={full[0]} ({full[0] / SAMPLE_RATE:.2f}s) "
            f"last={full[-1]} ({full[-1] / SAMPLE_RATE:.2f}s)"
        )
    for start in range(0, len(samples), window):
        chunk = samples[start : start + window]
        lines.append(
            f"  {start / SAMPLE_RATE:5.2f}s peak={frame_peak(chunk):6d} rms={frame_rms(chunk):6d}"
        )
    return lines


def write_pcm16_wav(path: Path, pcm: bytes, *, rate: int = SAMPLE_RATE, channels: int = 1) -> None:
    """Write int16 PCM as a WAV file (used for the raw debug copy and the final input)."""
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(rate)
        handle.writeframes(pcm)
