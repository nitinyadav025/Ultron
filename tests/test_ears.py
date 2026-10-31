"""Offline tests for Phase 3 local ears (faster-whisper). No model, no microphone, no network."""

from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from assistant.config import load_config
from assistant.interfaces.voice import VoiceController
from assistant.speech.ears import NO_SPEECH, EarsError, WhisperEars, load_wav_16k_mono


def _write_wav(path: Path, pcm: bytes, rate: int = 16000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return path


def _tone(count: int = 1600, value: int = 8000) -> bytes:
    data = np.tile(np.array([value, -value], dtype="<i2"), count // 2)
    return data.tobytes()


class FakeModel:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append({"audio": audio, **kwargs})
        segments = iter([SimpleNamespace(text=t) for t in self.texts])
        return segments, SimpleNamespace(language="en")


def _cfg(tmp_path: Path, *, with_model: bool = True):
    cfg = load_config()
    cfg.backend = "local"
    cfg.whisper_model_dir = tmp_path / "whisper"
    cfg.whisper_model_dir.mkdir(parents=True, exist_ok=True)
    if with_model:
        (cfg.whisper_model_dir / "model.bin").write_bytes(b"fake-weights")
    return cfg


def test_wav_loader_returns_mono_float32_at_16k(tmp_path: Path) -> None:
    wav = _write_wav(tmp_path / "a.wav", _tone())
    audio = load_wav_16k_mono(wav)
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert audio.size == 1600
    assert audio.max() <= 1.0


def test_wav_loader_downmixes_stereo_and_resamples_48k(tmp_path: Path) -> None:
    stereo = np.zeros((4800, 2), dtype="<i2")
    stereo[:, 0] = 4000
    stereo[:, 1] = 4000
    wav = _write_wav(tmp_path / "b.wav", stereo.tobytes(), rate=48000, channels=2)
    audio = load_wav_16k_mono(wav)
    assert audio.ndim == 1
    assert abs(audio.size - 1600) <= 2  # 4800 samples @48k ≈ 1600 @16k


def test_missing_model_reports_the_download_command(tmp_path: Path) -> None:
    ears = WhisperEars(_cfg(tmp_path, with_model=False))
    assert ears.is_downloaded() is False
    with pytest.raises(EarsError, match="download-models"):
        ears.load()


def test_model_loads_once_and_uses_cpu_int8_offline(tmp_path: Path) -> None:
    calls: list[Path] = []

    def factory(model_dir: Path) -> FakeModel:
        calls.append(model_dir)
        return FakeModel(["hello"])

    ears = WhisperEars(_cfg(tmp_path), model_factory=factory)
    first = ears.load()
    second = ears.load()
    assert first is second
    assert calls == [tmp_path / "whisper"]

    # The real factory must ask faster-whisper for CPU int8 and never hit the network.
    import inspect

    from assistant.speech import ears as ears_module

    source = inspect.getsource(ears_module._default_model_factory)
    assert 'device="cpu"' in source
    assert 'compute_type="int8"' in source
    assert "local_files_only=True" in source


def test_transcribe_passes_the_phase3_decode_settings(tmp_path: Path) -> None:
    model = FakeModel([" list files ", "in my documents "])
    ears = WhisperEars(_cfg(tmp_path), model_factory=lambda _d: model)
    wav = _write_wav(tmp_path / "c.wav", _tone())

    ok, text = ears.transcribe(wav)
    assert ok is True
    assert text == "list files in my documents"

    call = model.calls[0]
    assert isinstance(call["audio"], np.ndarray)  # arrays bypass the broken av.decode path
    assert call["language"] == "en"
    assert call["beam_size"] == 1
    assert call["vad_filter"] is True
    assert call["condition_on_previous_text"] is False


def test_hotwords_hint_uses_real_app_names_and_tool_words(tmp_path: Path) -> None:
    from assistant.speech.ears import build_hotwords, load_app_names

    names = load_app_names()
    assert "firefox" in names and "text editor" in names

    hotwords = build_hotwords()
    for word in ("firefox", "files", "terminal", "volume", "mute", "list files", "open"):
        assert word in hotwords
    # Kept small: faster-whisper truncates hints at half its max token length.
    assert len(hotwords) < 400

    model = FakeModel(["open firefox"])
    ears = WhisperEars(_cfg(tmp_path), model_factory=lambda _d: model)
    ears.transcribe(_write_wav(tmp_path / "e.wav", _tone()))
    assert model.calls[0]["hotwords"] == hotwords


def test_hotwords_can_be_overridden_by_config(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.whisper_hotwords = "firefox, spotify"
    model = FakeModel(["open firefox"])
    ears = WhisperEars(cfg, model_factory=lambda _d: model)
    ears.transcribe(_write_wav(tmp_path / "f.wav", _tone()))
    assert model.calls[0]["hotwords"] == "firefox, spotify"


def test_missing_apps_yaml_never_breaks_transcription(tmp_path: Path) -> None:
    from assistant.speech.ears import build_hotwords

    hotwords = build_hotwords(tmp_path / "nope.yaml")
    assert "volume" in hotwords  # falls back to the tool words


def test_silence_and_blank_tokens_are_not_speech(tmp_path: Path) -> None:
    ears = WhisperEars(_cfg(tmp_path), model_factory=lambda _d: FakeModel([]))
    empty = _write_wav(tmp_path / "empty.wav", b"")
    assert ears.transcribe(empty) == (False, NO_SPEECH)

    blank = _write_wav(tmp_path / "blank.wav", _tone())
    ears_blank = WhisperEars(_cfg(tmp_path), model_factory=lambda _d: FakeModel([" [BLANK_AUDIO] "]))
    assert ears_blank.transcribe(blank) == (False, NO_SPEECH)


def test_missing_audio_file_is_reported(tmp_path: Path) -> None:
    ears = WhisperEars(_cfg(tmp_path), model_factory=lambda _d: FakeModel([]))
    assert ears.transcribe(tmp_path / "nope.wav") == (False, "Audio file missing.")


def test_local_voice_controller_uses_ears_and_never_the_cloud(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    voice = VoiceController(config=cfg, adapter=None)
    assert voice.ears is not None
    voice.ears = WhisperEars(cfg, model_factory=lambda _d: FakeModel(["open brave"]))

    ok_load, load_msg = voice.load_ears()
    assert ok_load is True
    assert "base.en" in load_msg

    wav = _write_wav(tmp_path / "d.wav", _tone())
    ok, text = voice.transcribe_audio(wav_path=wav)
    assert (ok, text) == (True, "open brave")


def test_gemini_backend_keeps_cloud_transcription(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.backend = "gemini"
    voice = VoiceController(config=cfg, adapter=None)
    assert voice.ears is None
    ok, msg = voice.transcribe_audio(wav_path=tmp_path / "missing.wav")
    assert ok is False and msg == "Audio file missing."


def test_download_command_writes_into_the_project_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import faster_whisper.utils as fw_utils

    from assistant.speech.ears import download_whisper_model

    seen: dict[str, Any] = {}

    def fake_download(size_or_id: str, output_dir: str | None = None) -> str:
        seen["id"] = size_or_id
        seen["dir"] = output_dir
        Path(output_dir or ".").mkdir(parents=True, exist_ok=True)
        (Path(output_dir or ".") / "model.bin").write_bytes(b"weights")
        return output_dir or "."

    monkeypatch.setattr(fw_utils, "download_model", fake_download)
    cfg = _cfg(tmp_path, with_model=False)
    cfg.whisper_model_dir = tmp_path / "downloaded"

    messages: list[str] = []
    saved = download_whisper_model(cfg, progress=messages.append)

    assert seen["id"] == cfg.whisper_model
    assert Path(seen["dir"]) == tmp_path / "downloaded"
    assert (saved / "model.bin").is_file()
    assert WhisperEars(cfg).is_downloaded() is True
    assert any("downloading" in m for m in messages)


def test_voice_live_disabled_on_local_backend(tmp_path: Path) -> None:
    local = _cfg(tmp_path)
    assert local.voice_live_enabled is False
    local.backend = "gemini"
    assert local.voice_live_enabled is True
