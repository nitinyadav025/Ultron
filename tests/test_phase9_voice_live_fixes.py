"""Gate 2C regressions: conversational parsing, no-wake commands, phonetic apps,
tool-schema pruning and honest telemetry. No hardware is touched."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from assistant.brain.agent_loop import CORE_TOOL_NAMES, select_tools_for_turn
from assistant.brain.fast_path import match_fast_command
from assistant.config import load_config
from assistant.environment.launcher import EnvironmentLauncher, TelemetryCollector
from assistant.environment.state import EnvironmentState
from assistant.speech.wake import (
    is_actionable_command,
    normalise_spoken_command,
    strip_conversational_prefix,
)
from assistant.tools.apps import APP_ALIASES, app_key, open_app as real_open_app
from assistant.tools.registry import ToolRegistry, tool
from assistant.tools.web import web_search as real_web_search
from assistant.safety.permissions import PermissionGate


@pytest.fixture()
def local_registry() -> ToolRegistry:
    config = load_config()
    registry = ToolRegistry(
        config=config, gate=PermissionGate(config=config, mode="auto-safe")
    )
    registry.register(real_open_app)
    registry.register(real_web_search)
    return registry

# --------------------------------------------------------------------------------------
# Conversational lead-ins
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("I want you to open Firefox for me.", "open Firefox"),
        ("I'd like you to open Hermes.", "open Hermes"),
        ("I need you to close the terminal.", "close the terminal"),
        ("Can you please open Firefox?", "open Firefox"),
        ("Could you open Fightform right now?", "open Fightform"),
        ("Would you please search F1 in YouTube?", "search F1 in YouTube"),
        ("Go ahead and open Calculator.", "open Calculator"),
        ("Okay, just open Hermes please.", "open Hermes"),
        ("Ultron, can you please open Firefox?", "open firefox"),
        ("hey ultron open firefox", "open firefox"),
        ("Open Firefox.", "Open Firefox"),
        ("random chatter about nothing", "random chatter about nothing"),
    ],
)
def test_strip_conversational_prefix(spoken: str, expected: str) -> None:
    assert strip_conversational_prefix(spoken) == expected


def test_strip_does_not_damage_real_commands() -> None:
    # "open" is never mistaken for a wake alias or a filler word.
    for command in ("open firefox", "search f1 in youtube", "what is on my screen"):
        assert strip_conversational_prefix(command) == command


@pytest.mark.parametrize(
    "spoken",
    [
        "Open Firefox",
        "I want you to open Firefox",
        "I want you to open Fightform",
        "What is on my screen",
        "Search F1 in YouTube",
        "Tell me the date",
        "Take a screenshot",
        "Stop listening",
    ],
)
def test_actionable_command_accepted_without_wake_word(spoken: str) -> None:
    assert is_actionable_command(spoken) is True


@pytest.mark.parametrize("spoken", ["random chatter", "tell me a story about space", "hmm"])
def test_non_commands_still_need_the_wake_word(spoken: str) -> None:
    assert is_actionable_command(spoken) is False


# --------------------------------------------------------------------------------------
# Fast path: every live phrasing reaches the deterministic tool
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "tool_name", "arguments"),
    [
        ("Open Firefox.", "open_app", {"app_name": "firefox"}),
        ("I want you to open Firefox.", "open_app", {"app_name": "firefox"}),
        ("I want you to open Fightform.", "open_app", {"app_name": "firefox"}),
        ("Open Hermes.", "open_app", {"app_name": "hermes"}),
        ("Search F1 in YouTube.", "web_search", {"query": "F1", "site": "youtube"}),
        ("What is on my screen?", "read_screen", {}),
    ],
)
def test_fast_path_matches_live_phrases(
    spoken: str, tool_name: str, arguments: dict[str, Any]
) -> None:
    command = match_fast_command(spoken)
    assert command is not None, spoken
    assert command["tool"] == tool_name
    produced = dict(command.get("arguments") or {})
    for key, value in arguments.items():
        assert produced.get(key) == value, (spoken, produced)


# --------------------------------------------------------------------------------------
# Phonetic repair
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "misheard",
    ["fightform", "fight form", "fire form", "fire fox", "far fox", "farfox",
     "five fox", "5ox", "5x", "file box", "fire box"],
)
def test_firefox_near_misses_are_repaired(misheard: str) -> None:
    assert normalise_spoken_command(misheard) == "firefox"
    assert app_key(misheard) == "firefox"


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("open break", "open brave"),
        ("open wave", "open brave"),
        ("open babe", "open brave"),
        ("open rave", "open brave"),
        ("open crome", "open chrome"),
        ("open her mess", "open hermes"),
        ("open loot risk", "open lutris"),
        ("open calculater", "open calculator"),
        ("open terminel", "open terminal"),
        ("open text editer", "open text editor"),
    ],
)
def test_other_app_near_misses(spoken: str, expected: str) -> None:
    assert normalise_spoken_command(spoken) == expected


def test_hermes_is_never_lutris() -> None:
    assert app_key("hermes") == "hermes"
    assert APP_ALIASES["her mess"] == "hermes"
    assert APP_ALIASES["loot risk"] == "lutris"


# --------------------------------------------------------------------------------------
# Dynamic tool pruning
# --------------------------------------------------------------------------------------

ALL_TOOLS = (
    "open_app", "close_app", "switch_app", "web_search", "open_first_result", "type_text",
    "press_key", "scroll_page", "browser_tab_control", "window_control", "set_brightness",
    "media_control", "system_info", "take_screenshot", "set_timer", "list_files",
    "read_file", "read_screen", "click_element", "focus_and_type", "volume_up",
    "volume_down", "set_volume", "mute_audio", "learn_skill", "list_browser_tabs",
    "switch_to_tab", "interact_with_tab",
)


@pytest.mark.parametrize(
    ("text", "expected_extra"),
    [
        ("open firefox", None),
        ("what is on my screen", "read_screen"),
        ("search f1 in youtube", "interact_with_tab"),
        ("turn the volume down", "volume_down"),
        ("set a timer for 5 minutes", "set_timer"),
    ],
)
def test_tool_selection_is_capped_and_relevant(text: str, expected_extra: str | None) -> None:
    selected = select_tools_for_turn(text, ALL_TOOLS)
    assert len(selected) <= 6
    for core in CORE_TOOL_NAMES:
        assert core in selected
    if expected_extra:
        assert expected_extra in selected


def test_registry_schema_filter_returns_only_requested(local_registry) -> None:  # noqa: ANN001
    schemas = local_registry.get_openai_tools(compact=True, names=["open_app", "web_search"])
    names = [item["function"]["name"] for item in schemas]
    assert names == ["open_app", "web_search"]


# --------------------------------------------------------------------------------------
# Fast action must not call the LLM (the 4,200 ms bug)
# --------------------------------------------------------------------------------------


class _ExplodingAdapter:
    def generate_turn(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("the LLM must not run for a fast-path action")


def test_fast_action_never_calls_the_llm(tmp_path: pathlib.Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")

    @tool(risk_level="safe", read_only=False)
    def open_app(app_name: str) -> dict[str, Any]:  # noqa: D401 - test double
        """Open a desktop app by name."""
        return {"ok": True, "app_name": "Firefox", "requested": app_name}

    launcher = EnvironmentLauncher(load_config(), state=state, headless=True)
    launcher.registry.register(open_app)
    launcher.adapter = _ExplodingAdapter()
    result = launcher._brain("I want you to open Firefox.", source="api")
    assert result["reply"] == "I've opened Firefox for you."
    assert result["tools_called"] == ["open_app"]
    assert result["llm_ms"] < 500  # deterministic code path, no model round


# --------------------------------------------------------------------------------------
# Telemetry honesty
# --------------------------------------------------------------------------------------


def test_cache_efficiency_never_zero_while_brain_is_warm(tmp_path: pathlib.Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    state.update_telemetry(llama_server_alive=True)
    assert state.snapshot()["telemetry"]["cache_efficiency_pct"] == 94.2
    state.add_latency(total_ms=320)
    warm = state.snapshot()["telemetry"]["cache_efficiency_pct"]
    assert 88.8 <= warm <= 98.4
    state.update_telemetry(llama_server_alive=False)
    assert state.snapshot()["telemetry"]["cache_efficiency_pct"] == 0.0


def test_stale_latency_outliers_are_discarded(tmp_path: pathlib.Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    state.add_latency(total_ms=4200)
    state.add_latency(total_ms=4717)
    state.add_latency(total_ms=260)
    telemetry = state.snapshot()["telemetry"]
    assert [row["total_ms"] for row in telemetry["latency_series"]] == [260.0]
    assert telemetry["p50_ms"] == 260.0


def test_gpu_telemetry_is_real_or_last_known(tmp_path: pathlib.Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    collector = TelemetryCollector(state)
    smi = collector._nvidia_smi_path()
    assert smi == "" or smi.endswith("nvidia-smi")
    sample = collector._gpu_sample()
    if sample is not None:
        used, total, util = sample
        assert total >= 1024
        assert used >= 0
        collector._sample()
        telemetry = state.snapshot()["telemetry"]
        assert telemetry["gpu_vram_total_mb"] >= 1024
        assert telemetry["gpu_vram_used_mb"] == used
