"""Offline tests for Phase 3 local ears (faster-whisper). No model, no microphone, no network."""

from __future__ import annotations

import logging
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from assistant.config import load_config
from assistant.interfaces.voice import VoiceController
from assistant.speech.ears import (
    NO_SPEECH,
    UNCLEAR_SPEECH,
    EarsError,
    WhisperEars,
    load_wav_16k_mono,
    transcript_is_unreliable,
)


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
    assert call["beam_size"] == 2
    assert call["best_of"] == 2
    assert call["temperature"] == 0.0
    assert call["vad_filter"] is True
    assert call["vad_parameters"] == {
        "min_silence_duration_ms": 250,
        "speech_pad_ms": 200,
    }
    assert call["condition_on_previous_text"] is False
    # Phase 8 screen-primed vocabulary: the core names are always included, never a long
    # on-screen paragraph (that was the old auto-hotword echo bug).
    assert call["hotwords"].startswith("Ultron, Firefox, Brave, GitHub")
    assert len(call["hotwords"]) <= 240
    assert call["initial_prompt"] == _cfg(tmp_path).whisper_initial_prompt
    assert "Ultron" in call["initial_prompt"] and "YouTube" in call["initial_prompt"]
    assert "prompt" not in call


def test_initial_prompt_can_be_disabled(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.whisper_initial_prompt = ""
    model = FakeModel(["open firefox"])
    ears = WhisperEars(cfg, model_factory=lambda _d: model)
    ears.transcribe(_write_wav(tmp_path / "g.wav", _tone()))
    assert "initial_prompt" not in model.calls[0]


def test_hotwords_are_off_when_dynamic_priming_is_disabled(tmp_path: Path) -> None:
    # config/apps.yaml exists and used to be turned into a hotword list automatically.
    from assistant.config import PROJECT_ROOT

    assert (PROJECT_ROOT / "config" / "apps.yaml").is_file()

    cfg = _cfg(tmp_path)
    cfg.whisper_dynamic_hotwords = False
    model = FakeModel(["open firefox"])
    ears = WhisperEars(cfg, model_factory=lambda _d: model)
    ears.transcribe(_write_wav(tmp_path / "e.wav", _tone()))

    assert "hotwords" not in model.calls[0]


def test_hotwords_can_be_overridden_by_config(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.whisper_hotwords = "firefox, spotify"
    model = FakeModel(["open firefox"])
    ears = WhisperEars(cfg, model_factory=lambda _d: model)
    ears.transcribe(_write_wav(tmp_path / "f.wav", _tone()))
    assert model.calls[0]["hotwords"] == "firefox, spotify"


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


def test_whisper_never_receives_a_file_path_or_file_object(tmp_path: Path) -> None:
    """PyAV 19 breaks faster-whisper 1.2.1 on file paths: only float32 arrays are allowed."""
    model = FakeModel(["mute"])
    ears = WhisperEars(_cfg(tmp_path), model_factory=lambda _d: model)

    ears.transcribe(_write_wav(tmp_path / "array.wav", _tone()))

    audio = model.calls[0]["audio"]
    assert not isinstance(audio, (str, Path)), "a file path would hit the broken av.open call"
    assert isinstance(audio, np.ndarray)
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert audio.flags["C_CONTIGUOUS"]


# --------------------------------------------------------------------------------------
# "Say it again" guard (faster-whisper confidence + plain text rules)
# --------------------------------------------------------------------------------------


def _seg(text: str, avg_logprob: float = -0.2, no_speech_prob: float = 0.05, compression_ratio: float = 0.7) -> Any:
    return SimpleNamespace(
        text=text,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
        compression_ratio=compression_ratio,
    )


class ConfidenceModel:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append({"audio": audio, **kwargs})
        return iter(self.rows), SimpleNamespace(language="en")


def _ears_with(tmp_path: Path, rows: list[Any]) -> WhisperEars:
    model = ConfidenceModel(rows)
    return WhisperEars(_cfg(tmp_path), model_factory=lambda _d: model)


def test_clean_clip_confidence_is_accepted(tmp_path: Path) -> None:
    # Worst values measured on the six good /tmp/cmp2 clips.
    ears = _ears_with(
        tmp_path,
        [_seg("list files in my download folder", avg_logprob=-0.65, no_speech_prob=0.066, compression_ratio=0.81)],
    )
    ok, text = ears.transcribe(_write_wav(tmp_path / "good.wav", _tone()))
    assert ok is True
    assert text == "list files in my download folder"


def test_garbled_low_confidence_asks_to_say_it_again(tmp_path: Path) -> None:
    # One bad signal is never enough (a real live attempt scored -1.087); two are.
    ears = _ears_with(
        tmp_path,
        [_seg("Turn, board, volume to, cut, triple, percent", avg_logprob=-1.4, compression_ratio=2.7)],
    )
    assert ears.transcribe(_write_wav(tmp_path / "bad.wav", _tone())) == (False, UNCLEAR_SPEECH)


def test_one_bad_signal_is_rejected_again(tmp_path: Path) -> None:
    # With clean audio the limits are safe again, so one clear bad signal rejects.
    # Exact scores of the old live attempt: -1.087 / 0.065 / 0.76.
    ears = _ears_with(
        tmp_path,
        [_seg("Where is the MMAS folder?", avg_logprob=-1.087, no_speech_prob=0.065, compression_ratio=0.76)],
    )
    assert ears.transcribe(_write_wav(tmp_path / "real.wav", _tone())) == (False, UNCLEAR_SPEECH)
    assert ears.last_confidence["verdict"].startswith("avg_logprob=")


def test_good_clip_and_live_scores_are_accepted() -> None:
    from assistant.speech.ears import guard_reason

    good_clips = [
        (-0.141, 0.012, 0.704),
        (-0.325, 0.066, 0.800),
        (-0.227, 0.059, 0.619),
        (-0.650, 0.052, 0.385),
        (-0.166, 0.020, 0.814),
        (-0.220, 0.042, 0.692),
    ]
    live_turns = [
        (-0.089, 0.082, 1.233),
        (-0.107, 0.018, 0.742),
        (-0.129, 0.051, 0.771),
        (-0.144, 0.022, 0.742),
        (-0.150, 0.034, 0.814),
        (-0.151, 0.026, 0.966),
        (-0.224, 0.101, 0.805),
        (-0.343, 0.065, 0.579),
    ]
    for avg, no_speech, compression in good_clips + live_turns:
        segment = _seg("some real words", avg_logprob=avg, no_speech_prob=no_speech, compression_ratio=compression)
        assert guard_reason("some real words", [segment]) == ""


def test_high_no_speech_probability_is_rejected(tmp_path: Path) -> None:
    ears = _ears_with(tmp_path, [_seg("mute", no_speech_prob=0.90)])
    assert ears.transcribe(_write_wav(tmp_path / "ns.wav", _tone())) == (False, UNCLEAR_SPEECH)


def test_two_mildly_bad_signals_are_rejected(tmp_path: Path) -> None:
    ears = _ears_with(tmp_path, [_seg("list files", avg_logprob=-1.2, no_speech_prob=0.7)])
    assert ears.transcribe(_write_wav(tmp_path / "two.wav", _tone())) == (False, UNCLEAR_SPEECH)


def test_guard_limits_are_configurable(tmp_path: Path) -> None:
    from assistant.speech.ears import GuardLimits

    cfg = _cfg(tmp_path)
    model = ConfidenceModel([_seg("mute", avg_logprob=-1.1)])
    ears = WhisperEars(cfg, model_factory=lambda _d: model)
    # Strict default: -1.1 is below -1.0, so it is rejected.
    assert ears.transcribe(_write_wav(tmp_path / "cfg1.wav", _tone())) == (False, UNCLEAR_SPEECH)

    # Loosen the limit: the same score is now accepted.
    ears._guard_limits = lambda: GuardLimits(min_avg_logprob=-1.2)
    assert ears.transcribe(_write_wav(tmp_path / "cfg2.wav", _tone()))[0] is True


def test_earlier_build_garbled_strings_are_still_rejected() -> None:
    from assistant.speech.ears import guard_reason

    cases = [
        ("Turn, board, volume to, cut, triple, percent", _seg("x", avg_logprob=-1.4, compression_ratio=2.7)),
        ("mute, mute, unmute", _seg("x", avg_logprob=-0.2)),
        ("List files, see in my down download for 4-fold editor", _seg("x", avg_logprob=-1.4, no_speech_prob=0.7)),
    ]
    for text, segment in cases:
        assert guard_reason(text, [segment]) != "", text


def test_high_compression_ratio_is_rejected(tmp_path: Path) -> None:
    ears = _ears_with(tmp_path, [_seg("mute", compression_ratio=3.1)])
    assert ears.transcribe(_write_wav(tmp_path / "cr.wav", _tone())) == (False, UNCLEAR_SPEECH)


def test_repeated_word_loop_is_rejected_even_with_good_confidence(tmp_path: Path) -> None:
    ears = _ears_with(tmp_path, [_seg("mute, mute, unmute")])
    assert ears.transcribe(_write_wav(tmp_path / "loop.wav", _tone())) == (False, UNCLEAR_SPEECH)


def test_transcript_without_a_real_word_is_rejected(tmp_path: Path) -> None:
    assert transcript_is_unreliable(", ,", []) is True
    assert transcript_is_unreliable("a", []) is True
    assert transcript_is_unreliable("mute", []) is False



def test_local_voice_controller_uses_ears_and_never_the_cloud(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    voice = VoiceController(config=cfg, adapter=None)
    assert voice.ears is not None
    voice.ears = WhisperEars(cfg, model_factory=lambda _d: FakeModel(["open brave"]))

    ok_load, load_msg = voice.load_ears()
    assert ok_load is True
    assert "small.en" in load_msg

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


def test_private_transcription_keeps_the_words_out_of_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ultron.ears")
    cfg = _cfg(tmp_path)
    cfg.backend = "local"
    ears = WhisperEars(cfg, model_factory=lambda _d: FakeModel(["secret words"]))
    samples = np.zeros(16000, dtype=np.float32)

    ok, text = ears.transcribe_samples(samples, log_text=False)

    assert ok is True and text == "secret words"  # returned in memory
    assert "secret words" not in caplog.text       # but never written anywhere
    assert "text_chars=12" in caplog.text
