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
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320 samples = 20 ms
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH

# The HDA mic emits a short full-scale thump (~250-350 ms) when its PipeWire node wakes
# from suspend. It is not the user, so it must never count as speech start.
WARMUP_MS = 400
# Noise floor measured from the first frames (clipped frames excluded).
CALIBRATION_MS = 200
PRE_ROLL_MS = 300
MIN_SPEECH_MS = 200  # clicks and bumps shorter than this are not speech
NOISE_MULTIPLIER = 3.0
ABS_MIN_FRAME_RMS = 220  # int16 RMS; below this a frame is always silence
CLIP_LEVEL = 31000  # a frame whose peak reaches this is (near) full scale

# Trailing silence kept after speech so the last word is not clipped.
TRAILING_SILENCE_MS = 300


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
    reason: str = "error"  # silence | timeout | max_length | cancelled | source_ended | no_audio
    duration_ms: int = 0
    speech_ms: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.pcm) and self.saw_speech


# --------------------------------------------------------------------------------------
# Real source: pw-record (PipeWire) or arecord (ALSA) streaming raw PCM on stdout
# --------------------------------------------------------------------------------------


class PwRecordSource:
    """Streams 16 kHz mono s16 PCM from ``pw-record`` (or ``arecord``) over a pipe."""

    def __init__(self, rate: int = SAMPLE_RATE) -> None:
        self.rate = rate
        self.error = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._cmd: list[str] = []

    @staticmethod
    def command(rate: int = SAMPLE_RATE) -> list[str]:
        if shutil.which("pw-record"):
            return [
                "pw-record",
                "--raw",
                "--format=s16",
                f"--rate={rate}",
                "--channels=1",
                "-",
            ]
        if shutil.which("arecord"):
            return ["arecord", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw"]
        raise RecorderError("Neither pw-record (pipewire-utils) nor arecord (alsa-utils) is installed.")

    def start(self) -> None:
        self._cmd = self.command(self.rate)
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


def record_until_silence(
    source: AudioSource,
    *,
    start_timeout_s: float = 8.0,
    silence_ms: int = 1000,
    max_seconds: float = 30.0,
    min_speech_ms: int = MIN_SPEECH_MS,
    pre_roll_ms: int = PRE_ROLL_MS,
    status: Callable[[str], None] | None = None,
) -> Recording:
    """Capture speech until ``silence_ms`` of quiet after it started.

    Timing is measured in *audio* time (frames received), so tests with synthetic audio
    run at full speed while the live path behaves like a wall clock.

    ``status`` receives "Listening... speak now", "Heard you..." and "Got it.".
    """
    notify = status or (lambda _msg: None)
    notify("Listening... speak now")

    source.start()
    # Keep ~300 ms *before* the first word. The 200 ms used to confirm speech is part of
    # the ring buffer, so the pre-roll window must be larger than 300 ms.
    pre_roll_frames = max(1, (pre_roll_ms + min_speech_ms) // FRAME_MS)
    pre_roll: list[array.array] = []
    collected: list[array.array] = []
    calibration: list[int] = []

    noise_floor = float(ABS_MIN_FRAME_RMS)
    calibrated = False
    in_speech = False
    speech_run = 0
    silence_run = 0
    speech_frames = 0
    audio_ms = 0
    empty_reads = 0
    carry = b""
    reason = "no_audio"

    started = time.monotonic()

    try:
        while True:
            chunk = source.read(FRAME_BYTES, timeout=0.25)
            if chunk is None:
                reason = "source_ended" if in_speech else "no_audio"
                break
            if not chunk:
                empty_reads += 1
                # A live device that has delivered nothing for ~5 s is broken.
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
                clipped = peak >= CLIP_LEVEL

                # --- noise floor calibration (skip the device wake-up thump) ---
                if not calibrated:
                    if not clipped and level > 0:
                        calibration.append(level)
                    if audio_ms >= CALIBRATION_MS:
                        noise_floor = float(median(calibration) or ABS_MIN_FRAME_RMS)
                        calibrated = True

                threshold = max(noise_floor * NOISE_MULTIPLIER, ABS_MIN_FRAME_RMS)
                is_speech = level >= threshold and not (clipped and audio_ms <= WARMUP_MS)

                if not in_speech:
                    pre_roll.append(frame)
                    if len(pre_roll) > pre_roll_frames:
                        pre_roll.pop(0)
                    if is_speech:
                        speech_run += 1
                        speech_frames += 1
                    else:
                        speech_run = 0
                        if calibrated and audio_ms > WARMUP_MS:
                            # Track slow room-noise drift, never track speech.
                            noise_floor = 0.9 * noise_floor + 0.1 * level
                    if speech_run * FRAME_MS >= min_speech_ms:
                        in_speech = True
                        notify("Heard you...")
                        collected.extend(pre_roll)
                        pre_roll = []
                    elif audio_ms >= int(start_timeout_s * 1000):
                        reason = "timeout"
                        break
                else:
                    collected.append(frame)
                    if is_speech:
                        silence_run = 0
                        speech_frames += 1
                    else:
                        silence_run += 1
                    if silence_run * FRAME_MS >= silence_ms:
                        reason = "silence"
                        break
                    if audio_ms >= int(max_seconds * 1000):
                        reason = "max_length"
                        break

            if reason in ("timeout", "silence", "max_length"):
                break
    finally:
        source.stop()

    if not in_speech:
        return Recording(pcm=b"", saw_speech=False, reason=reason, duration_ms=audio_ms, speech_ms=speech_frames * FRAME_MS)

    if reason == "silence":
        # Cut the long pause, keeping only a short tail after the last speech frame.
        threshold = max(noise_floor * NOISE_MULTIPLIER, ABS_MIN_FRAME_RMS)
        last_speech = 0
        for index, frame in enumerate(collected):
            if frame_rms(frame) >= threshold:
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
    )


def has_speech_energy(
    pcm: bytes,
    *,
    min_speech_ms: int = MIN_SPEECH_MS,
    start_skip_ms: int = WARMUP_MS,
) -> bool:
    """Offline check used by fixed-window mode: does this buffer contain real speech?"""
    frames = split_frames(pcm)
    if not frames:
        return False
    levels = [frame_rms(frame) for frame in frames]
    floor = float(percentile(levels, 0.25) or ABS_MIN_FRAME_RMS)
    threshold = max(floor * NOISE_MULTIPLIER, ABS_MIN_FRAME_RMS)
    run = 0
    for index, level in enumerate(levels):
        clipped_now = frame_peak(frames[index]) >= CLIP_LEVEL and index * FRAME_MS <= start_skip_ms
        if level >= threshold and not clipped_now:
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
    rest_median = median(levels[limit:]) or ABS_MIN_FRAME_RMS

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
SAFE_OUTPUT_PEAK = int(32767 * 0.7)
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
    """Even out the recording on a *robust* level instead of the single loudest sample.

    Old behaviour: gain = target / max_sample. One click (or the mic wake-up thump) made
    the whole recording quiet, and quiet room noise was amplified up to 8x.

    New behaviour: the level is the 75th percentile of the 20 ms frame RMS values, so a
    few bad frames cannot set the gain. Loud audio is turned down, quiet speech is boosted
    at most 3x, audio with no speech is never boosted, and the result never clips.
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
    peaks = [frame_peak(samples[i : i + FRAME_SAMPLES]) for i in range(0, len(samples), FRAME_SAMPLES)]
    info.speech_level = percentile(levels, 0.75)

    if info.speech_level <= 0 or info.before_peak <= 0:
        info.after_peak, info.after_rms = info.before_peak, info.before_rms
        return pcm, info

    gain = float(target_rms) / float(info.speech_level)
    if not has_speech:
        gain = min(gain, 1.0)  # never amplify room noise
        info.notes.append("no speech in buffer; not boosting")
    gain = min(gain, max_boost)

    # Protect the *body* of the speech, not a single stray click: the 95th percentile of
    # frame peaks ignores one bad frame, so a click cannot turn the whole recording down.
    if gain > 1.0:
        robust_peak = percentile(peaks, 0.95) or info.before_peak
        gain = min(gain, float(SAFE_OUTPUT_PEAK) / float(robust_peak))
    if abs(gain - 1.0) < 0.02:
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
