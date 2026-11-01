"""The dataclass defaults must match what load_config uses when a key is absent."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import AssistantConfig, load_config


def test_voice_timing_defaults_are_consistent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "VOICE_SILENCE_MS",
        "VOICE_RECORD_SILENCE_MS",
        "VOICE_START_TIMEOUT_S",
        "VOICE_MAX_SECONDS",
        "LISTEN_EARLY_CHECK_MS",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config(env_path=tmp_path / "missing.env", settings_path=tmp_path / "missing.yaml")

    assert AssistantConfig().voice_silence_ms == 1450
    assert AssistantConfig().voice_record_silence_ms == 1350
    assert AssistantConfig().voice_start_timeout_s == 10.0
    assert cfg.voice_silence_ms == 1450
    assert cfg.voice_record_silence_ms == 1350
    assert cfg.voice_start_timeout_s == 10.0
    assert cfg.voice_max_seconds == 30.0
    assert AssistantConfig().listen_early_check_ms == 1050
    assert cfg.listen_early_check_ms == 1050
