"""Hard guard: with BACKEND=local nothing may construct a Gemini client, check a key, or call the API."""

from __future__ import annotations

import io
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import assistant.brain as brain
import assistant.doctor as doctor
from assistant.brain.local_client import LocalAdapter
from assistant.brain.prompts import build_system_prompt
from assistant.config import load_config
from assistant.interfaces.voice import VoiceController
from assistant.speech.ears import WhisperEars


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("Gemini was used with BACKEND=local")


def _local_cfg(tmp_path: Path) -> Any:
    cfg = load_config()
    cfg.backend = "local"
    cfg.api_key = ""  # no key at all
    cfg.whisper_model_dir = tmp_path / "whisper"
    cfg.whisper_model_dir.mkdir(parents=True, exist_ok=True)
    (cfg.whisper_model_dir / "model.bin").write_bytes(b"w")
    cfg.piper_voice = "en_US-lessac-medium"
    cfg.piper_voice_dir = tmp_path / "piper"
    cfg.piper_voice_dir.mkdir(parents=True, exist_ok=True)
    (cfg.piper_voice_dir / "en_US-lessac-medium.onnx").write_bytes(b"o")
    (cfg.piper_voice_dir / "en_US-lessac-medium.onnx.json").write_bytes(b"{}")
    return cfg


class FakeModel:
    def transcribe(self, _audio: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return iter([SimpleNamespace(text="open brave")]), SimpleNamespace(language="en")


class FakeMouth:
    def synthesize_wav(self, _text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 50)
        return buf.getvalue()


def test_local_backend_builds_only_the_local_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import google.genai as genai

    monkeypatch.setattr(brain, "GeminiAdapter", _boom)
    monkeypatch.setattr(genai, "Client", _boom)
    cfg = _local_cfg(tmp_path)
    adapter = brain.build_adapter(cfg)
    assert isinstance(adapter, LocalAdapter)
    assert cfg.has_valid_key is False  # nothing ever asks for a key on the local backend


def test_local_voice_turn_never_touches_gemini(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import google.genai as genai

    monkeypatch.setattr(genai, "Client", _boom)
    cfg = _local_cfg(tmp_path)

    voice = VoiceController(config=cfg, adapter=None)
    voice.ears = WhisperEars(cfg, model_factory=lambda _d: FakeModel())
    voice.mouth = FakeMouth()
    voice._play_wav_file = lambda _p: (True, "pw-play")  # type: ignore[assignment]

    wav = tmp_path / "x.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 160)
    assert voice.transcribe_audio(wav) == (True, "open brave")
    ok, info = voice.speak_sync("Done.")
    assert ok is True and "Piper" in info


def test_local_voice_test_never_builds_gemini(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _local_cfg(tmp_path)
    monkeypatch.setattr(doctor, "GeminiAdapter", _boom)

    class FakeVoiceController:
        def __init__(self, config: Any, adapter: Any = None, status_callback: Any = None) -> None:
            assert adapter is None, "local voice-test must not need a Gemini adapter"
            self.last_peak = 1
            self.last_rms = 1
            self.last_peak_before = 32768
            self.last_rms_before = 9000

        def load_ears(self) -> tuple[bool, str]:
            return True, "fake ears"

        def record_push_to_talk(self, _duration: float) -> tuple[bool, str]:
            return True, "fake.wav"

        def record(self) -> tuple[bool, str]:
            return self.record_push_to_talk(0.0)

        def transcribe_audio(self) -> tuple[bool, str]:
            return True, "open brave"

        def load_mouth(self) -> tuple[bool, str]:
            return True, "fake mouth"

        def speak_sync(self, _text: str) -> tuple[bool, str]:
            return True, "fake speech"

    monkeypatch.setattr(doctor, "VoiceController", FakeVoiceController)
    assert doctor.run_voice_test(cfg) == 0


def test_local_system_prompt_asks_for_one_short_sentence(tmp_path: Path) -> None:
    from assistant.brain.prompts import VOICE_RULE

    local = _local_cfg(tmp_path)
    assert VOICE_RULE in build_system_prompt(local, "ask")
    assert "one or two complete spoken sentences" in VOICE_RULE

    local.backend = "gemini"
    assert VOICE_RULE not in build_system_prompt(local, "ask")
