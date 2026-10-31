"""Verify the local backend caps tool rounds at 3 and then forces a final answer."""

from __future__ import annotations

from typing import Any

from google.genai import types

from assistant.brain.agent_loop import AgentLoop
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.config import load_config
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.tools.registry import ToolRegistry, tool

EXEC_COUNT = {"n": 0}


@tool(risk_level="safe", read_only=False)
def dummy_ping(times: int = 1) -> dict:
    """Test-only tool that counts executions."""
    EXEC_COUNT["n"] += 1
    return {"ok": True, "times": times}


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

    result = loop.run_task("ping")
    assert result.ok is True
    assert result.reply == "Summary done."
    assert EXEC_COUNT["n"] == 3  # exactly three tool rounds, never a 4th
    assert adapter.tools_seen[-1] is None  # final turn runs without tools
    assert adapter.calls == 4
    # Local generation stays short: 128 tokens for the first call, 96 after a tool ran.
    assert adapter.max_output_tokens_seen == [128, 96, 96, 96]
