"""`main.py wake-train`: learn how this user's voice actually says "Ultron".

The USER runs it. It opens ONE microphone stream, waits for the warm-up, then asks for 16
clips one cue at a time. Each clip is transcribed four ways so the user can see which
setting hears the wake word best:

* base.en plain (the production setting)
* base.en with hotwords="Ultron"
* base.en with vad_filter off
* small.en plain, only when ``models/faster-whisper-small.en`` exists

No default changes from this table. It writes ``workspace/wake_aliases.json`` with the
first one to three words Whisper produced and their counts; ``WAKE_ALIASES_FILE`` loads it.
The audio is saved in ``workspace/wake_clips/`` because the user starts this command.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from assistant.config import WORKSPACE_DIR, AssistantConfig
from assistant.interfaces.listen import ContinuousReader, UtteranceDetector
from assistant.speech.ears import WhisperEars
from assistant.speech.recorder import (
    FRAME_BYTES,
    PwRecordSource,
    pcm16_samples,
    write_pcm16_wav,
)

logger = logging.getLogger("ultron.waketrain")

CUES: tuple[tuple[str, int], ...] = (
    ("Ultron", 8),
    ("Ultron open firefox", 4),
    ("Hey Ultron", 4),
)
VARIANTS = ("plain", "hotwords", "no_vad", "small_en")


def _prefixes(text: str, counts: dict[str, int]) -> None:
    words = [word for word in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split() if word]
    for size in (1, 2, 3):
        if len(words) >= size:
            prefix = " ".join(words[:size])
            counts[prefix] = counts.get(prefix, 0) + 1


def _small_ears(config: AssistantConfig) -> WhisperEars | None:
    directory = Path(config.project_root) / "models" / "faster-whisper-small.en"
    if not (directory / "model.bin").is_file():
        return None
    small_config = copy.copy(config)
    small_config.whisper_model = "small.en"
    small_config.whisper_model_dir = directory
    return WhisperEars(small_config)


def _microphone_capture(config: AssistantConfig) -> tuple[Callable[[], bytes | None], Callable[[], None], Callable[[], None]]:
    """One stream for the whole session; each call returns the next utterance."""
    source = PwRecordSource(target=(config.mic_source_name or ""))
    reader = ContinuousReader(source)
    detector = UtteranceDetector(
        warmup_ms=config.recorder_warmup_ms,
        warmup_max_ms=config.recorder_warmup_max_ms,
        min_snr_db=config.voice_min_snr_db,
        silence_ms=config.voice_silence_ms,
        early_ms=config.listen_early_check_ms,
        max_seconds=10.0,
    )
    reader.start()
    carry = b""

    def capture() -> bytes | None:
        nonlocal carry
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            chunk = reader.get(0.25)
            if chunk is None:
                return None
            if not chunk:
                continue
            carry += chunk
            while len(carry) >= FRAME_BYTES:
                raw, carry = carry[:FRAME_BYTES], carry[FRAME_BYTES:]
                event = detector.feed(pcm16_samples(raw))
                if event in ("early", "final"):
                    pcm = detector.take_pcm()
                    detector.reset()
                    return pcm
        return None

    def wait_ready() -> None:
        nonlocal carry
        deadline = time.monotonic() + 10.0
        while not detector.ready and time.monotonic() < deadline:
            chunk = reader.get(0.25)
            if chunk is None:
                return
            if not chunk:
                continue
            carry += chunk
            while len(carry) >= FRAME_BYTES:
                raw, carry = carry[:FRAME_BYTES], carry[FRAME_BYTES:]
                detector.feed(pcm16_samples(raw))

    def close() -> None:
        reader.stop()

    return capture, close, wait_ready


def run_wake_train(
    config: AssistantConfig,
    *,
    capture: Callable[[], bytes | None] | None = None,
    ears: Any | None = None,
    small_ears: Any | None = None,
    prompt: Callable[[str], None] = print,
    clips_dir: Path | None = None,
    aliases_path: Path | None = None,
) -> int:
    clips = Path(clips_dir) if clips_dir else WORKSPACE_DIR / "wake_clips"
    aliases_file = Path(aliases_path) if aliases_path else WORKSPACE_DIR / "wake_aliases.json"
    clips.mkdir(parents=True, exist_ok=True)

    if ears is None:
        ears = WhisperEars(config)
        try:
            ears.load()
        except Exception as exc:
            prompt(f"[ERROR] Ears unavailable: {exc}")
            return 1
    if small_ears is None:
        small_ears = _small_ears(config)
    close = None
    wait_ready = None
    if capture is None:
        capture, close, wait_ready = _microphone_capture(config)

    counts: dict[str, int] = {}
    timings: dict[str, list[float]] = {name: [] for name in VARIANTS}
    rows: list[tuple[str, dict[str, str], dict[str, float]]] = []
    total = sum(count for _phrase, count in CUES)
    index = 0

    try:
        if wait_ready is not None:
            wait_ready()  # do not ask for a cue during the opening pop
        for phrase, repeat in CUES:
            for _ in range(repeat):
                index += 1
                prompt(f"[{index}/{total}] Say: {phrase}")
                pcm = capture()
                if not pcm:
                    prompt("  (no speech, skipped)")
                    continue
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                write_pcm16_wav(clips / f"wake_{stamp}_{index:02d}.wav", pcm)
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                texts: dict[str, str] = {}
                seconds: dict[str, float] = {}
                variants: list[tuple[str, Any, dict[str, Any]]] = [
                    ("plain", ears, {}),
                    ("hotwords", ears, {"hotwords": "Ultron"}),
                    ("no_vad", ears, {"vad_filter": False}),
                ]
                if small_ears is not None:
                    variants.append(("small_en", small_ears, {}))
                for name, engine, options in variants:
                    started = time.monotonic()
                    ok, text = engine.transcribe_samples(samples, log_text=False, **options)
                    seconds[name] = time.monotonic() - started
                    texts[name] = text if ok else f"<rejected: {text}>"
                    timings[name].append(seconds[name])
                _prefixes(texts["plain"].replace("<rejected: ", "").rstrip(">"), counts)
                rows.append((phrase, texts, seconds))
    except KeyboardInterrupt:
        prompt("\nStopped.")
    finally:
        if close is not None:
            close()

    prompt("")
    prompt(f"{'cue':26} {'plain':28} {'hotwords':28} {'no_vad':28} {'small.en':28}")
    for phrase, texts, _seconds in rows:
        prompt(
            f"{phrase[:26]:26} {texts.get('plain', '')[:28]:28} {texts.get('hotwords', '')[:28]:28} "
            f"{texts.get('no_vad', '')[:28]:28} {texts.get('small_en', '')[:28]:28}"
        )
    prompt("")
    for name in VARIANTS:
        samples = timings[name]
        if samples:
            prompt(f"average {name:9}: {sum(samples) / len(samples):.2f} s over {len(samples)} clips")
        else:
            prompt(f"average {name:9}: not available")
    if small_ears is None:
        prompt("small.en: models/faster-whisper-small.en is not installed (skipped).")

    aliases_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "clips": len(rows),
        "aliases": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
    }
    aliases_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt(f"Saved {len(rows)} clips in {clips} and aliases in {aliases_file}.")
    prompt("No default was changed. Review the table, then set WAKE_ALIASES if you want.")
    return 0
