"""Verify the local backend caps tool rounds at 3 and then forces a final answer."""

from __future__ import annotations

from typing import Any

import pytest
from google.genai import types

from assistant.brain.agent_loop import AgentLoop
from assistant.environment.state import get_state
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.config import load_config
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.tools.registry import ToolRegistry, tool
from assistant.tools.volume import TIMED_MUTE_REPLY, WHAT_MUTE_REPLY, WHAT_VOLUME_REPLY

EXEC_COUNT = {"n": 0}
SET_VOLUME_COUNT = {"n": 0}
MUTE_CALLS: dict[str, Any] = {"n": 0, "args": []}


@pytest.fixture(autouse=True)
def _deterministic_tool_replies():
    """This module pins the code-built tool sentence; the LLM path is tested elsewhere."""
    state = get_state()
    previous = state.live_config.get("brain_replies", True)
    state.live_config["brain_replies"] = False
    yield
    state.live_config["brain_replies"] = previous


@tool(risk_level="safe", read_only=False)
def dummy_ping(times: int = 1) -> dict:
    """Test-only tool that counts executions."""
    EXEC_COUNT["n"] += 1
    return {"ok": True, "times": times}


@tool(risk_level="safe", read_only=False, name="set_volume")
def dummy_set_volume(percent: int = 50) -> dict:
    """Test-only absolute volume tool that counts executions."""
    SET_VOLUME_COUNT["n"] += 1
    return {"ok": True, "requested_percent": percent}


@tool(risk_level="safe", read_only=False, name="mute_audio")
def dummy_mute_audio(muted: bool = True) -> dict:
    """Test-only mute tool that records the explicit action it was asked to do."""
    MUTE_CALLS["n"] += 1
    MUTE_CALLS["args"].append(bool(muted))
    return {"ok": True, "muted": bool(muted)}


class FakeLocalAdapter:
    """Duck-typed stand-in for LocalAdapter that always asks for one more tool round."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self) -> None:
        self.calls = 0
        self.tools_seen: list[Any] = []
        self.max_output_tokens_seen: list[int | None] = []

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        self.calls += 1
        self.max_output_tokens_seen.append(max_output_tokens)
        self.tools_seen.append(tools)
        if tools is None:
            content = types.Content(role="model", parts=[types.Part.from_text(text="Summary done.")])
        else:
            content = types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(name="dummy_ping", args={"times": 1}, id=f"call_{self.calls}")
                    )
                ],
            )
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


def test_local_backend_caps_tool_rounds_at_three() -> None:
    EXEC_COUNT["n"] = 0
    cfg = load_config()
    cfg.backend = "local"

    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(dummy_ping)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    adapter = FakeLocalAdapter()
    loop = AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]

    result = loop.run_task("list files")
    assert result.ok is True
    assert result.reply == "Summary done."
    assert EXEC_COUNT["n"] == 3  # exactly three tool rounds, never a 4th
    assert adapter.tools_seen[-1] is None  # final turn runs without tools
    assert adapter.calls == 4
    # Phase 7: 220 tokens on every local call so spoken replies and type_text paragraphs
    # are never cut mid-sentence.
    assert adapter.max_output_tokens_seen == [220, 220, 220, 220]


class SetVolumeAdapter:
    """Calls set_volume(30) once, then answers with text."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self) -> None:
        self.calls = 0

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        self.calls += 1
        if self.calls == 1:
            content = types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name="set_volume", args={"percent": 30}, id="v1"))],
            )
        else:
            content = types.Content(role="model", parts=[types.Part.from_text(text="Done.")])
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


def _set_volume_loop(adapter: SetVolumeAdapter) -> AgentLoop:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(dummy_set_volume)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    return AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]


def test_set_volume_without_a_spoken_number_runs_no_tool() -> None:
    SET_VOLUME_COUNT["n"] = 0
    adapter = SetVolumeAdapter()

    result = _set_volume_loop(adapter).run_task("turn the volume up")

    assert result.reply == WHAT_VOLUME_REPLY
    assert SET_VOLUME_COUNT["n"] == 0  # nothing ran
    assert adapter.calls == 1  # no second LLM turn either


def test_set_volume_with_a_spoken_number_runs_the_tool() -> None:
    SET_VOLUME_COUNT["n"] = 0
    adapter = SetVolumeAdapter()

    _set_volume_loop(adapter).run_task("turn volume to thirty percent")

    assert SET_VOLUME_COUNT["n"] == 1


class MuteAdapter:
    """Calls mute_audio with a chosen (possibly wrong) bool, then answers with text."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, muted: bool = True) -> None:
        self.muted = muted
        self.calls = 0

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        self.calls += 1
        if self.calls == 1:
            content = types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(name="mute_audio", args={"muted": self.muted}, id="m1")
                    )
                ],
            )
        else:
            content = types.Content(role="model", parts=[types.Part.from_text(text="Done.")])
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


OPEN_APP_CALLS = {"n": 0}


@tool(risk_level="safe", read_only=False, name="open_app")
def dummy_open_app(app_name: str) -> dict:
    """Test-only app launcher that must not run for a question."""
    OPEN_APP_CALLS["n"] += 1
    return {"ok": True, "app_name": app_name}


class QuestionAppAdapter:
    """Calls open_app on a question, then answers with text once tools are removed."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self) -> None:
        self.calls = 0
        self.tools_seen: list[Any] = []

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        self.calls += 1
        self.tools_seen.append(tools)
        if self.calls == 1:
            content = types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="open_app", args={"app_name": "firefox"}, id="app1"
                        )
                    )
                ],
            )
        else:
            content = types.Content(
                role="model", parts=[types.Part.from_text(text="I am Ultron, ready to help.")]
            )
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


class NeverAdapter:
    """Fails the test if the LLM is called at all."""

    is_local = True
    usage_meter = LocalUsageMeter()
    calls = 0

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(self, *_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        NeverAdapter.calls += 1
        raise AssertionError("the LLM must not be called for a timed mute request")


def _mute_loop(adapter: Any) -> AgentLoop:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(dummy_mute_audio)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    return AgentLoop(cfg, adapter, registry, watchdog)


def _reset_mute() -> None:
    MUTE_CALLS["n"] = 0
    MUTE_CALLS["args"] = []


def test_mute_word_forces_muted_true_even_if_the_model_asked_to_unmute() -> None:
    _reset_mute()
    result = _mute_loop(MuteAdapter(muted=False)).run_task("mute")

    assert MUTE_CALLS == {"n": 1, "args": [True]}
    assert result.reply == "Audio is now muted."


def test_unmute_word_forces_muted_false_even_if_the_model_asked_to_mute() -> None:
    _reset_mute()
    result = _mute_loop(MuteAdapter(muted=True)).run_task("unmute")

    assert MUTE_CALLS == {"n": 1, "args": [False]}
    assert result.reply == "Audio is now unmuted."


def test_ambiguous_mute_and_unmute_asks_and_runs_nothing() -> None:
    _reset_mute()
    adapter = MuteAdapter()
    result = _mute_loop(adapter).run_task("mute unmute")

    assert result.reply == WHAT_MUTE_REPLY
    assert MUTE_CALLS["n"] == 0
    assert adapter.calls == 1  # only the model turn that proposed the call


def test_open_app_is_never_run_when_the_request_is_not_an_app_action() -> None:
    OPEN_APP_CALLS["n"] = 0
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(dummy_open_app)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    adapter = QuestionAppAdapter()
    loop = AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]

    result = loop.run_task("list files")

    assert result.ok is True
    assert "Ultron" in result.reply
    assert OPEN_APP_CALLS["n"] == 0  # the model's app call was refused
    assert adapter.calls == 2  # one refused tool turn, then a plain answer with no tools
    assert adapter.tools_seen[-1] is None


class ChatAdapter:
    """Records the one lean chat request and answers with text."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self) -> None:
        self.calls = 0
        self.tools_seen: list[Any] = []
        self.system_prompts: list[str] = []
        self.history_lengths: list[int] = []

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        self.calls += 1
        self.tools_seen.append(tools)
        self.system_prompts.append(system_instruction)
        self.history_lengths.append(len(contents) if isinstance(contents, list) else -1)
        content = types.Content(role="model", parts=[types.Part.from_text(text="Hello, I am Ultron.")])
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


def test_pure_chat_uses_one_lean_request_without_tools() -> None:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    adapter = ChatAdapter()
    loop = AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]

    result = loop.run_task("what can you do?")

    assert result.ok is True and result.reply == "Hello, I am Ultron."
    assert adapter.calls == 1
    assert adapter.tools_seen == [None]  # no tool schemas on a conversation turn
    assert "no tools are available" in adapter.system_prompts[0]
    # The Dashboard persona adds the user's name (Nitin) to the lean prompt.
    # The lean chat prompt is ~90 words (<110) and byte-identical across turns.
    assert len(adapter.system_prompts[0]) < 700
    assert len(adapter.system_prompts[0].split()) <= 110
    assert adapter.history_lengths[0] <= 8  # last four turns only


def test_timed_mute_asks_honestly_without_the_llm() -> None:
    _reset_mute()
    NeverAdapter.calls = 0

    result = _mute_loop(NeverAdapter()).run_task("Stay silent for 10 seconds.")

    assert result.reply == TIMED_MUTE_REPLY
    assert MUTE_CALLS["n"] == 0
    assert NeverAdapter.calls == 0


def test_yes_after_the_timed_mute_offer_mutes_now_without_the_llm() -> None:
    _reset_mute()
    NeverAdapter.calls = 0
    loop = _mute_loop(NeverAdapter())

    first = loop.run_task("Stay silent for 10 seconds.")
    assert first.reply == TIMED_MUTE_REPLY and MUTE_CALLS["n"] == 0

    second = loop.run_task("yes")
    assert MUTE_CALLS == {"n": 1, "args": [True]}
    assert second.reply == "Audio is now muted."
    assert NeverAdapter.calls == 0
