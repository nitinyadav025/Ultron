"""Optional clean-up before Whisper: high-pass + gentle stationary spectral gate.

Numpy only (no new packages). Both steps are deliberately mild:

* a high-pass removes the fan/air rumble below ``highpass_hz`` (default 100 Hz);
* a stationary spectral gate takes its noise profile from the quietest frames of the
  *same recording* (never from the voice), then lowers every frequency bin by at most
  ``strength`` (0.6 = at most ~8 dB). A fan is steady, so this removes its hiss without
  touching syllables.

Audio with no confirmed speech is never processed: the caller passes ``has_speech`` and
this module returns it untouched. The audio length is preserved exactly, so the 400 ms
pre-roll stays intact.
"""

from __future__ import annotations

import array
import math
import sys

import numpy as np

SAMPLE_RATE = 16000
FRAME = 512  # 32 ms analysis window
HOP = 256  # 50 % overlap
MAX_STRENGTH = 0.8


def _highpass(samples: np.ndarray, cutoff_hz: float, rate: int) -> np.ndarray:
    """One-pole high-pass, applied as x - lowpass(x) using an FFT convolution."""
    if cutoff_hz <= 0 or samples.size == 0:
        return samples
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    alpha = (1.0 / rate) / (rc + 1.0 / rate)
    length = min(samples.size, max(2, int(4.0 * rc * rate)))
    kernel = alpha * np.power(1.0 - alpha, np.arange(length, dtype=np.float64))
    kernel /= kernel.sum()

    size = 1 << int(math.ceil(math.log2(samples.size + length - 1)))
    low = np.fft.irfft(
        np.fft.rfft(samples.astype(np.float64), size) * np.fft.rfft(kernel, size), size
    )[: samples.size]
    return (samples.astype(np.float64) - low).astype(np.float32)


def _frames(samples: np.ndarray) -> np.ndarray:
    if samples.size < FRAME:
        padded = np.zeros(FRAME, dtype=np.float32)
        padded[: samples.size] = samples
        return padded[None, :]
    count = 1 + (samples.size - FRAME) // HOP
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    return samples[index]


def _overlap_add(frames: np.ndarray, total: int, window: np.ndarray) -> np.ndarray:
    out = np.zeros(total + FRAME, dtype=np.float64)
    norm = np.zeros(total + FRAME, dtype=np.float64)
    weight = window * window
    for index, frame in enumerate(frames):
        start = index * HOP
        out[start : start + FRAME] += frame * window
        norm[start : start + FRAME] += weight
    return (out / np.maximum(norm, 1e-8))[:total].astype(np.float32)


def denoise_float32(
    samples: np.ndarray,
    *,
    strength: float = 0.6,
    highpass_hz: float = 100.0,
    rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """Return cleaned float32 audio; never changes length, never clips, 0 = unchanged."""
    strength = max(0.0, min(MAX_STRENGTH, float(strength)))
    if strength <= 0.0 or samples.size == 0:
        return samples.astype(np.float32, copy=False)

    filtered = _highpass(samples, highpass_hz, rate)
    window = np.hanning(FRAME).astype(np.float32)
    # Pad both ends so overlap-add has full coverage at the real start/end (an
    # unpadded first frame divides by an almost-empty window sum and explodes).
    pad = FRAME + HOP
    padded = np.concatenate(
        [np.zeros(pad, dtype=np.float32), filtered.astype(np.float32), np.zeros(pad, dtype=np.float32)]
    )
    frames = _frames(padded)
    spectrum = np.fft.rfft(frames * window, axis=1)
    magnitude = np.abs(spectrum)

    # Noise profile from the quietest 20 % of frames: the voice is not in them.
    energy = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    quiet_count = max(1, int(round(0.2 * len(frames))))
    quiet = np.argsort(energy)[:quiet_count]
    noise_power = np.median(magnitude[quiet], axis=0) ** 2

    power = magnitude**2
    gain = 1.0 - strength * (noise_power / (power + noise_power + 1e-12))
    gain = np.clip(gain, 1.0 - strength, 1.0)
    # Smooth across time so the gate never chatters on a single frame.
    if len(gain) >= 3:
        smoothed = gain.copy()
        smoothed[1:-1] = (gain[:-2] + gain[1:-1] + gain[2:]) / 3.0
        gain = smoothed

    cleaned = np.fft.irfft(spectrum * gain, n=FRAME, axis=1).astype(np.float32)
    out = _overlap_add(cleaned, padded.size, window)
    # The gate only ever attenuates, so the result cannot be louder than the input.
    return out[pad : pad + samples.size].astype(np.float32, copy=False)


def denoise_pcm16(
    pcm: bytes,
    *,
    strength: float = 0.6,
    highpass_hz: float = 100.0,
    rate: int = SAMPLE_RATE,
) -> bytes:
    """Apply the clean-up to 16-bit little-endian PCM bytes and return PCM bytes."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return pcm
    samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0
    cleaned = denoise_float32(samples, strength=strength, highpass_hz=highpass_hz, rate=rate)
    out = array.array(
        "h",
        (max(-32768, min(32767, int(round(float(value) * 32767.0)))) for value in cleaned),
    )
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()
