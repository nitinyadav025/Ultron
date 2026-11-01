"""MIC_SOURCE_VOLUME: optional one-time microphone input level (fake wpctl, no system change)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from assistant.config import load_config
from assistant.speech.mic_input import DEFAULT_SOURCE, apply_mic_source_volume


def _runner(returncode: int = 0, stderr: str = "") -> tuple[Any, list[list[str]]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    return run, calls


def test_unset_value_does_nothing() -> None:
    run, calls = _runner()
    ok, message = apply_mic_source_volume("", runner=run, which=lambda _n: "/usr/bin/wpctl")
    assert ok is False
    assert "not set" in message
    assert calls == []


def test_sets_the_default_source_once() -> None:
    run, calls = _runner()
    ok, message = apply_mic_source_volume("0.21", runner=run, which=lambda _n: "/usr/bin/wpctl")

    assert ok is True
    assert "0.21" in message
    assert calls == [["wpctl", "set-volume", DEFAULT_SOURCE, "0.21"]]


def test_out_of_range_and_non_number_are_ignored() -> None:
    run, calls = _runner()
    assert apply_mic_source_volume("0.90", runner=run, which=lambda _n: "/usr/bin/wpctl")[0] is False
    assert apply_mic_source_volume("loud", runner=run, which=lambda _n: "/usr/bin/wpctl")[0] is False
    assert calls == []


def test_missing_wpctl_is_ignored() -> None:
    run, calls = _runner()
    ok, message = apply_mic_source_volume("0.21", runner=run, which=lambda _n: None)
    assert ok is False
    assert "wpctl" in message
    assert calls == []


def test_wpctl_failure_is_one_log_line(caplog: Any) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="ultron.mic")
    run, _calls = _runner(returncode=1, stderr="permission denied")
    ok, _message = apply_mic_source_volume("0.21", runner=run, which=lambda _n: "/usr/bin/wpctl")

    assert ok is False
    assert "permission denied" in caplog.text
    assert caplog.text.count("MIC_SOURCE_VOLUME") == 1


def test_config_reads_mic_source_volume(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MIC_SOURCE_VOLUME=0.21\n", encoding="utf-8")

    cfg = load_config(env_path=env_file, settings_path=tmp_path / "no-settings.yaml")

    assert cfg.mic_source_volume == "0.21"
