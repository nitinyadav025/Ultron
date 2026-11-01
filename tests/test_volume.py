"""Offline tests for the wpctl volume tools (no PipeWire required)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from assistant.config import load_config
from assistant.safety.permissions import PermissionGate
from assistant.tools.registry import ToolRegistry
from assistant.tools.volume import (
    MAX_STEP_PERCENT,
    TIMED_MUTE_REPLY,
    WHAT_MUTE_REPLY,
    WHAT_VOLUME_REPLY,
    asks_for_timed_mute,
    clamp_percent,
    clamp_step,
    mute_audio,
    mute_intent,
    numbers_in_text,
    percent_is_mentioned,
    set_volume,
    volume_down,
    volume_up,
)


def _install_fake_wpctl(monkeypatch: Any, current: str = "Volume: 0.50") -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((cmd, kwargs))
        if cmd[1] == "get-volume":
            return SimpleNamespace(returncode=0, stdout=current + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("assistant.tools.volume.subprocess.run", fake_run)
    return calls


def test_step_is_clamped_to_safe_range() -> None:
    assert clamp_step(999) == MAX_STEP_PERCENT
    assert clamp_step(1) == 5
    assert clamp_step("not-a-number") == 10


def test_volume_up_caps_at_100_percent(monkeypatch: Any) -> None:
    calls = _install_fake_wpctl(monkeypatch, current="Volume: 0.95")
    result = volume_up(50)  # clamps to 20%
    assert result["ok"] is True
    assert result["step_percent"] == 20
    assert result["volume_percent"] == 100
    set_cmd, kwargs = calls[-1]
    assert set_cmd == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.000", "--limit", "1.0"]
    assert "shell" not in kwargs and kwargs.get("check") is False


def test_volume_down_clamps_at_zero(monkeypatch: Any) -> None:
    calls = _install_fake_wpctl(monkeypatch, current="Volume: 0.03")
    result = volume_down(20)
    assert result["ok"] is True
    assert result["volume_percent"] == 0
    assert calls[-1][0] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.000", "--limit", "1.0"]


def test_volume_tools_are_registered_low_risk(monkeypatch: Any) -> None:
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    reg = ToolRegistry(config=cfg, gate=gate)
    for fn in (volume_up, volume_down, set_volume, mute_audio):
        spec = reg.register(fn)
        assert spec.risk_level == "safe"
    schemas = reg.get_openai_tools()
    names = {s["function"]["name"] for s in schemas}
    assert {"volume_up", "volume_down", "set_volume", "mute_audio"} <= names
    up = next(s for s in schemas if s["function"]["name"] == "volume_up")
    assert up["function"]["parameters"]["properties"]["step_percent"]["type"] == "integer"
    absolute = next(s for s in schemas if s["function"]["name"] == "set_volume")
    assert absolute["function"]["parameters"]["properties"]["percent"]["type"] == "integer"


def test_set_volume_clamps_to_0_100() -> None:
    assert clamp_percent(30) == 30
    assert clamp_percent(-5) == 0
    assert clamp_percent(150) == 100
    assert clamp_percent("loud") == 50


def test_numbers_are_read_from_digits_and_number_words() -> None:
    assert numbers_in_text("turn volume to thirty percent") == {30}
    assert numbers_in_text("turn volume to 30%") == {30}
    assert numbers_in_text("set the volume to fifty") == {50}
    assert 55 in numbers_in_text("set it to fifty-five")
    assert 25 in numbers_in_text("set it to twenty five")
    assert 100 in numbers_in_text("one hundred percent")
    assert numbers_in_text("turn the volume up") == set()


def test_percent_must_appear_in_the_transcript() -> None:
    assert percent_is_mentioned(30, "turn volume to thirty percent") is True
    assert percent_is_mentioned(30, "turn volume to 30") is True
    assert percent_is_mentioned(50, "fifty percent please") is True
    assert percent_is_mentioned(30, "turn the volume up") is False
    assert percent_is_mentioned(30, "turn volume to fifty percent") is False
    assert WHAT_VOLUME_REPLY == "What volume should I set? Say a number from 0 to 100."


def test_mute_intent_is_explicit_or_ambiguous() -> None:
    assert mute_intent("mute") == "mute"
    assert mute_intent("Mute the audio.") == "mute"
    assert mute_intent("unmute") == "unmute"
    assert mute_intent("mute unmute") is None
    assert mute_intent("turn the volume up") is None
    assert mute_intent("stay silent for 10 seconds") is None
    assert WHAT_MUTE_REPLY == "Do you want to mute or unmute?"


def test_timed_mute_requests_are_detected() -> None:
    assert asks_for_timed_mute("Stay silent for 10 seconds.") is True
    assert asks_for_timed_mute("mute for a minute") is True
    assert asks_for_timed_mute("mute") is False
    assert asks_for_timed_mute("turn the volume up for 10 seconds") is False
    assert TIMED_MUTE_REPLY == (
        "I can mute the audio, but I can't unmute it automatically after a time. "
        "Should I mute it now?"
    )


def test_set_volume_uses_wpctl_absolute_target(monkeypatch: Any) -> None:
    calls = _install_fake_wpctl(monkeypatch, current="Volume: 0.10")
    result = set_volume(30)
    assert result["ok"] is True
    assert result["requested_percent"] == 30
    assert result["volume_percent"] == 30
    set_cmd, kwargs = calls[-1]
    assert set_cmd == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.300", "--limit", "1.0"]
    assert "shell" not in kwargs and kwargs.get("check") is False

    # "turn volume to 0" must actually reach zero, not a capped step.
    calls.clear()
    mute_like_zero = set_volume(0)
    assert mute_like_zero["volume_percent"] == 0
    assert calls[-1][0] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.000", "--limit", "1.0"]

    calls.clear()
    clamped = set_volume(180)
    assert clamped["volume_percent"] == 100
    assert calls[-1][0][3] == "1.000"


def test_mute_and_unmute_use_wpctl_set_mute(monkeypatch: Any) -> None:
    calls = _install_fake_wpctl(monkeypatch)
    muted = mute_audio(True)
    assert muted == {"ok": True, "muted": True, "sink": "@DEFAULT_AUDIO_SINK@"}
    assert calls[-1][0] == ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"]

    unmuted = mute_audio(False)
    assert unmuted["muted"] is False
    assert calls[-1][0] == ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"]


def test_new_volume_tools_run_without_asking_in_auto_safe(monkeypatch: Any) -> None:
    _install_fake_wpctl(monkeypatch)
    cfg = load_config()
    asked: list[str] = []

    def never(_detail: str, _timeout: int) -> str:
        asked.append("asked")
        return "no"

    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=never)
    reg = ToolRegistry(config=cfg, gate=gate)
    for fn in (set_volume, mute_audio):
        reg.register(fn)

    assert reg.execute("set_volume", {"percent": 30})["ok"] is True
    assert reg.execute("mute_audio", {"muted": True})["ok"] is True
    assert asked == []  # never prompted in auto-safe mode
