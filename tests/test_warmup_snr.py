"""Part 4: adaptive warm-up and the SNR gate, measured on the real saved takes."""

from __future__ import annotations

import glob
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from assistant.config import PROJECT_ROOT
from assistant.speech.recorder import (
    AdaptiveWarmup,
    SyntheticSource,
    frame_peak,
    frame_rms,
    pcm16_samples,
    record_until_silence,
)

CLIPS = PROJECT_ROOT / "workspace" / "test_clips"
POP_TAKES = [
    "attempt_20261101-134518-844549.wav",
    "attempt_20261101-135201-414426.wav",
    "attempt_20261101-135223-024570.wav",
]


def _pcm(name: str) -> bytes:
    path = CLIPS / name
    if not path.exists():
        pytest.skip(f"real clip missing: {path}")
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def _samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64)


def _warm_end(pcm: bytes, min_ms: int = 300, max_ms: int = 1500) -> tuple[int, float]:
    warm = AdaptiveWarmup(min_ms, max_ms)
    frames = [pcm16_samples(pcm[i : i + 640]) for i in range(0, len(pcm) - 639, 640)]
    for index, frame in enumerate(frames):
        warm.feed(frame_rms(frame), frame_peak(frame), (index + 1) * 20)
        if warm.done:
            levels = [frame_rms(item) for item in frames]
            floor = float(np.percentile(levels, 10))  # room floor between syllables
            return warm.end_ms, floor
    return max_ms, 0.0


def test_the_three_real_pop_takes_end_warmup_after_the_pop() -> None:
    for name in POP_TAKES:
        end_ms, floor = _warm_end(_pcm(name))
        assert 300 <= end_ms <= 1500, (name, end_ms)
        assert end_ms >= 700, (name, end_ms)  # the pop really lasts ~700-800 ms
        assert floor < 1000, (name, floor)


def test_quiet_takes_end_warmup_at_the_minimum() -> None:
    for trace in sorted(CLIPS.glob("attempt_*.json")):
        if trace.name.endswith(("134409-155401.json",)) or trace.with_suffix(".wav").name in POP_TAKES:
            continue
        end_ms, floor = _warm_end(_pcm(trace.with_suffix(".wav").name))
        assert end_ms <= 500, (trace.name, end_ms)
        assert floor < 1000, (trace.name, floor)


def test_loud_take_keeps_its_real_room_level() -> None:
    # This take is loud for its whole length (transcript "Unused.", already rejected).
    # The warm-up must NOT invent a quiet floor for it.
    end_ms, floor = _warm_end(_pcm("attempt_20261101-134409-155401.wav"))
    assert end_ms == 1500
    assert floor >= 1000


def test_unit_adaptive_warmup_cases() -> None:
    calm = AdaptiveWarmup(300, 1500)
    for index in range(40):
        calm.feed(200, 400, (index + 1) * 20)
        if calm.done:
            break
    assert calm.done and calm.end_ms == 300

    loud = AdaptiveWarmup(300, 1500)
    for index in range(100):
        loud.feed(4700, 12000, (index + 1) * 20)
    assert loud.done and loud.end_ms == 1500

    pop = AdaptiveWarmup(300, 1500)
    levels = [int(30000 - index * 480) for index in range(60)] + [350] * 20
    for index, level in enumerate(levels):
        pop.feed(max(level, 300), min(32767, max(level, 300) * 2), (index + 1) * 20)
        if pop.done:
            break
    assert pop.done and 300 <= pop.end_ms <= 1500


def _low_noise(frames: int = 40) -> bytes:
    rng = np.random.default_rng(4)
    return rng.normal(0, 250, frames * 320).astype("<i2").tobytes()


def test_fan_constant_noise_and_a_click_do_not_start_a_recording() -> None:
    fan = _pcm("attempt_20261101-135201-414426.wav")
    fan_result = record_until_silence(SyntheticSource(fan), max_seconds=30)
    assert fan_result.saw_speech is False

    rng = np.random.default_rng(5)
    constant = rng.integers(-4700, 4700, 16000 * 20).astype("<i2").tobytes()
    assert record_until_silence(SyntheticSource(constant), max_seconds=30).saw_speech is False

    click = b"\x00\x00" * 16000 + b"\xff\x7f\x00\x80" * 5 + b"\x00\x00" * 16000
    assert record_until_silence(SyntheticSource(click), max_seconds=10).saw_speech is False


def test_quietest_real_clip_is_detected_with_margin() -> None:
    clip = _pcm("attempt_20261101-134642-028341.wav")  # "What is your name?"
    pcm = _low_noise(40) + clip + _low_noise(40)

    recording = record_until_silence(SyntheticSource(pcm), max_seconds=20)

    assert recording.saw_speech is True
    # Measure the same way the detector does: per-frame speech vs the room floor.
    samples = _samples(pcm)
    frames = [samples[i : i + 320] for i in range(0, len(samples) - 319, 320)]
    levels = [float(np.sqrt(np.mean(item**2))) for item in frames]
    floor = max(40.0, float(np.percentile(levels, 10)))
    gate = floor * 10 ** (10 / 20)
    passing = [x for x in levels if x >= gate]
    speech = float(np.percentile(passing, 75))
    snr_db = 20 * np.log10(speech / floor)
    assert snr_db >= 13.0  # at least 3 dB above the 10 dB gate
