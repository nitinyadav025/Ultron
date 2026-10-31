"""Offline tests for Phase 4 mouth (Piper). No real voice, no audio device, no network."""

from __future__ import annotations

import io
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from assistant.config import load_config
from assistant.interfaces.voice import VoiceController
from assistant.speech.mouth import MouthError, PiperMouth, split_sentences


class FakeChunk:
    def __init__(self, pcm: bytes, rate: int = 22050) -> None:
        self.sample_rate = rate
        self.sample_width = 2
        self.sample_channels = 1
        self._pcm = pcm

    @property
    def audio_int16_bytes(self) -> bytes:
        return self._pcm


class FakeVoice:
    """Stands in for PiperVoice: constant-amplitude tone, instant synthesis."""

    def __init__(self, amplitude: int = 30000, samples: int = 4410, rate: int = 22050) -> None:
        self.config = SimpleNamespace(sample_rate=rate)
        self.amplitude = amplitude
        self.samples = samples
        self.texts: list[str] = []

    def synthesize(self, text: str, syn_config: Any = None) -> list[FakeChunk]:
        self.texts.append(text)
        tone = np.tile(np.array([self.amplitude, -self.amplitude], dtype="<i2"), self.samples // 2)
        return [FakeChunk(tone.tobytes(), self.config.sample_rate)]


class FakeMouth:
    """Minimal stand-in for PiperMouth used by the VoiceController streaming path."""

    def __init__(self, rate: int = 22050, on_synth: Any = None) -> None:
        self.rate = rate
        self.texts: list[str] = []
        self.on_synth = on_synth

    def synthesize_wav(self, text: str) -> bytes:
        self.texts.append(text)
        if self.on_synth is not None:
            self.on_synth(text)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.rate)
            wf.writeframes(b"\x00\x00" * 100)
        return buf.getvalue()


def _cfg(tmp_path: Path) -> Any:
    cfg = load_config()
    cfg.backend = "local"
    cfg.piper_voice = "en_US-lessac-medium"
    cfg.piper_voice_dir = tmp_path / "piper"
    cfg.piper_voice_dir.mkdir(parents=True, exist_ok=True)
    (cfg.piper_voice_dir / "en_US-lessac-medium.onnx").write_bytes(b"onnx")
    (cfg.piper_voice_dir / "en_US-lessac-medium.onnx.json").write_bytes(b"{}")
    return cfg


def test_split_sentences_keeps_order_and_caps_length() -> None:
    assert split_sentences("Done.") == ["Done."]
    assert split_sentences("") == []
    parts = split_sentences("Done. I found twelve files in your Downloads folder. Anything else?")
    assert parts == ["Done.", "I found twelve files in your Downloads folder.", "Anything else?"]
    long_text = " ".join(f"Sentence number {i} is here." for i in range(10))
    assert len(split_sentences(long_text)) == 6


def test_missing_voice_reports_the_download_command(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    (cfg.piper_voice_dir / "en_US-lessac-medium.onnx").unlink()
    mouth = PiperMouth(cfg)
    assert mouth.is_downloaded() is False
    with pytest.raises(MouthError, match="download-models"):
        mouth.load()


def test_voice_loads_once_and_uses_cpu_defaults(tmp_path: Path) -> None:
    calls: list[Path] = []

    def factory(model: Path, config_path: Path) -> FakeVoice:
        calls.append(model)
        assert config_path.name.endswith(".onnx.json")
        return FakeVoice()

    mouth = PiperMouth(_cfg(tmp_path), voice_factory=factory)
    assert mouth.load() is mouth.load()
    assert calls == [tmp_path / "piper" / "en_US-lessac-medium.onnx"]
    assert mouth.sample_rate() == 22050


def test_wav_is_clean_padded_and_correctly_formatted(tmp_path: Path) -> None:
    mouth = PiperMouth(_cfg(tmp_path), voice_factory=lambda _m, _c: FakeVoice())
    wav = mouth.synthesize_wav("Hello there.")
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getframerate() == 22050  # the voice's real rate, not a guess
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")

    peak = int(np.abs(samples).max())
    assert peak <= 31130  # 0.95 ceiling: headroom, so no clipping
    assert peak >= 28000  # but still loud
    lead = int(22050 * 0.11)
    tail = int(22050 * 0.19)
    assert int(np.abs(samples[:lead]).max()) == 0  # nothing cut off at the start
    assert int(np.abs(samples[-tail:]).max()) == 0  # nothing cut off at the end


def test_volume_is_applied_without_clipping(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.voice_volume = 0.5
    mouth = PiperMouth(cfg, voice_factory=lambda _m, _c: FakeVoice(amplitude=32000))
    wav = mouth.synthesize_wav("Quiet please.")
    with wave.open(io.BytesIO(wav), "rb") as wf:
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    assert int(np.abs(samples).max()) <= 32767
    assert int(np.abs(samples).max()) < 17000


def test_empty_synthesis_is_an_error(tmp_path: Path) -> None:
    empty = SimpleNamespace(config=SimpleNamespace(sample_rate=22050), synthesize=lambda *_a, **_k: [])
    mouth = PiperMouth(_cfg(tmp_path), voice_factory=lambda _m, _c: empty)
    with pytest.raises(MouthError, match="no audio"):
        mouth.synthesize_wav("Hello.")


def test_local_reply_is_spoken_by_piper_only(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    voice = VoiceController(config=cfg, adapter=None)
    voice.mouth = FakeMouth()
    played: list[str] = []
    voice._play_wav_file = lambda path: (played.append(path.name), (True, "pw-play"))[1]  # type: ignore[assignment]
    voice._speak_fallback_local = lambda _t: (_ for _ in ()).throw(AssertionError("spd-say must not run"))  # type: ignore[assignment]

    ok, info = voice.speak_sync("Done. I found twelve files.")
    assert ok is True
    assert "Piper" in info and "pw-play" in info
    assert played == ["voice_output_0.wav", "voice_output_1.wav"]


def test_first_sentence_plays_while_later_ones_generate(tmp_path: Path) -> None:
    """The producer must not finish synthesizing everything before playback starts."""
    cfg = _cfg(tmp_path)
    voice = VoiceController(config=cfg, adapter=None)

    first_play = threading.Event()
    late_synth_saw_playback = {"value": False, "timed_out": False}

    def on_synth(text: str) -> None:
        if text.startswith("Third"):
            got = first_play.wait(timeout=2.0)
            late_synth_saw_playback["timed_out"] = not got
            late_synth_saw_playback["value"] = got

    mouth = FakeMouth(on_synth=on_synth)
    voice.mouth = mouth
    played: list[str] = []

    def fake_play(path: Path) -> tuple[bool, str]:
        played.append(path.name)
        first_play.set()
        time.sleep(0.2)
        return True, "pw-play"

    voice._play_wav_file = fake_play  # type: ignore[assignment]
    # _speak_local_piper() is the streaming unit; speak_sync() trims to two sentences first.
    ok, _ = voice._speak_local_piper(
        "First sentence here. Second sentence here. Third sentence here. Fourth sentence here."
    )
    assert ok is True
    assert played == ["voice_output_0.wav", "voice_output_1.wav", "voice_output_2.wav", "voice_output_3.wav"]
    assert late_synth_saw_playback["timed_out"] is False


def test_ctrl_c_while_speaking_stops_and_does_not_hang(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    voice = VoiceController(config=cfg, adapter=None)
    voice.mouth = FakeMouth()

    def boom(_path: Path) -> tuple[bool, str]:
        raise KeyboardInterrupt

    voice._play_wav_file = boom  # type: ignore[assignment]
    with pytest.raises(KeyboardInterrupt):
        voice.speak_sync("Hello there. This is a second sentence.")
    voice.stop_speaking()  # must be a safe no-op afterwards
