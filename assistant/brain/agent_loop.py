"""Agent Loop: plan -> act -> observe -> verify -> report.
Enforces 25-step cap, wall-clock cap, 3-failure stop, parallel tool calls, and live redirection.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from google.genai import types

from assistant.brain.gemini_client import GeminiAdapter, TokenUsageStats
from assistant.brain.local_client import LocalAdapter
from assistant.brain.prompts import build_system_prompt
from assistant.brain.reply_style import short_plain_reply
from assistant.brain.tool_output import compact_tool_result
from assistant.brain.tool_replies import code_reply_for_tool, fallback_reply, is_lead_in
from assistant.config import AssistantConfig
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.redact import redact_secrets
from assistant.tools.apps import resolve_app_launch_command
from assistant.tools.registry import ToolRegistry

# Local generation caps: a tool call needs far fewer tokens than the 512 default, and a
# follow-up turn must stay a one-sentence answer (long paragraphs were the 20 s reply).
LOCAL_MAX_TOKENS_FIRST = 128
LOCAL_MAX_TOKENS_FOLLOWUP = 96


@dataclass
class AgentTaskResult:
    ok: bool
    reply: str
    steps_taken: int
    usage: TokenUsageStats = field(default_factory=TokenUsageStats)
    aborted_reason: str = ""
    dry_run: bool = False


class AgentLoop:
    """Coordinates multi-step tool execution, verification, and live user redirection."""

    def __init__(
        self,
        config: AssistantConfig,
        adapter: GeminiAdapter | LocalAdapter,
        registry: ToolRegistry,
        watchdog: WatchdogMonitor,
        step_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.registry = registry
        self.watchdog = watchdog
        self.step_callback = step_callback or (lambda msg: None)
        self.history: list[types.Content] = []
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._redirect_queue: queue.Queue[str] = queue.Queue()

    def pause(self) -> None:
        self._pause_event.clear()
        self.step_callback("Paused. Click Resume or type a new instruction.")

    def resume(self) -> None:
        self._pause_event.set()
        self.step_callback("Resumed.")

    def redirect(self, new_instruction: str) -> None:
        self._redirect_queue.put(new_instruction)
        self._pause_event.set()
        self.step_callback(f"Redirected: {new_instruction}")

    def _preview(self, fn_name: str, args: dict[str, Any]) -> str:
        if fn_name == "open_app" and "app_name" in args:
            cmd, _ = resolve_app_launch_command(str(args["app_name"]))
            return f"open_app({args['app_name']!r}) -> {' '.join(cmd)}"
        return f"{fn_name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"

    def _finalize_reply(self, text: str, last_tool: tuple[str, dict[str, Any]] | None = None) -> str:
        """Local voice replies are forced to one short plain sentence (no markdown, no sizes).

        If a tool just ran, a fact we already have in code wins over the model's wording:
        ``list_files`` is always answered with the real count and folder name, and a pure
        lead-in ("Here are the first items...") is replaced by a safe template.
        """
        if self.config.backend != "local":
            return text
        if last_tool is not None:
            built = code_reply_for_tool(last_tool[0], last_tool[1])
            if built:
                return built
        shaped = short_plain_reply(text, max_chars=self.config.voice_reply_max_chars)
        if last_tool is not None and is_lead_in(shaped):
            return fallback_reply(last_tool[0])
        return shaped

    def _local_tool_payload(self, fn_name: str, res: dict[str, Any]) -> dict[str, Any]:
        """Local model gets a short fenced summary instead of the full (duplicated) result."""
        return {"ok": bool(res.get("ok", False)), "result": compact_tool_result(fn_name, res)}

    def _generation_limits(self, tool_rounds: int) -> dict[str, Any]:
        """Cap local generation so a turn cannot ramble for 500 tokens (Gemini is unchanged)."""
        if self.config.backend != "local":
            return {}
        limit = LOCAL_MAX_TOKENS_FIRST if tool_rounds == 0 else LOCAL_MAX_TOKENS_FOLLOWUP
        return {"max_output_tokens": limit}

    def run_task(self, user_input: str, dry_run: bool = False) -> AgentTaskResult:
        cleaned = user_input.strip()
        if cleaned.lower().startswith("plan only"):
            dry_run = True
            cleaned = cleaned[9:].lstrip(": ").strip() or user_input
        if dry_run:
            cleaned = f"[DRY RUN / PLAN ONLY] Print the step-by-step plan without executing tools for: {cleaned}"

        self.watchdog.start_task()
        self.registry.gate.tainted_by_untrusted = False
        usage_acc = TokenUsageStats(daily_total=self.adapter.usage_meter.get_today_total(), daily_cap=self.config.daily_token_cap)
        self.history.append(types.Content(role="user", parts=[types.Part.from_text(text=redact_secrets(cleaned))]))

        failures = 0
        steps = 0
        tool_rounds = 0
        last_tool: tuple[str, dict[str, Any]] | None = None
        local_tool_round_cap = 3 if self.config.backend == "local" else None
        try:
            while steps < self.config.max_steps:
                while not self._pause_event.wait(0.2):
                    ok, reason = self.watchdog.check_ok()
                    if not ok:
                        return AgentTaskResult(False, reason, steps, usage_acc, reason)

                while not self._redirect_queue.empty():
                    redir = self._redirect_queue.get_nowait()
                    self.history.append(types.Content(role="user", parts=[types.Part.from_text(text=f"[LIVE REDIRECT]: {redact_secrets(redir)}")]))

                ok, reason = self.watchdog.check_ok()
                if not ok:
                    return AgentTaskResult(False, reason, steps, usage_acc, reason)

                steps = self.watchdog.record_step()
                thinking_note = "" if self.config.backend == "local" else f" | {self.config.active_thinking_level}"
                self.step_callback(
                    f"[Step {steps}/{self.config.max_steps}] Thinking ({self.config.current_model}{thinking_note})..."
                )

                if dry_run:
                    tools_payload = None
                elif local_tool_round_cap is not None and tool_rounds >= local_tool_round_cap:
                    tools_payload = None  # small local model: force a final answer after 3 tool rounds
                elif self.config.backend == "local":
                    tools_payload = self.registry.get_openai_tools()
                else:
                    tools_payload = self.registry.get_gemini_tools()
                resp, turn_u = self.adapter.generate_turn(
                    contents=self.history,
                    system_instruction=build_system_prompt(self.config, self.registry.gate.mode),
                    tools=tools_payload,
                    **self._generation_limits(tool_rounds),
                )
                usage_acc.prompt_tokens += turn_u.prompt_tokens
                usage_acc.candidate_tokens += turn_u.candidate_tokens
                usage_acc.thought_tokens += turn_u.thought_tokens
                usage_acc.total_tokens += turn_u.total_tokens
                usage_acc.daily_total = turn_u.daily_total

                if not resp.candidates:
                    return AgentTaskResult(False, "No response candidates returned.", steps, usage_acc)

                cand = resp.candidates[0]
                if cand.content is not None:
                    self.history.append(cand.content)

                fn_calls = resp.function_calls or []
                if not fn_calls:
                    self.step_callback("Idle — task complete.")
                    return AgentTaskResult(
                        True,
                        self._finalize_reply(resp.text or "Done.", last_tool),
                        steps,
                        usage_acc,
                        dry_run=dry_run,
                    )

                resp_parts: list[types.Part] = []
                human_notes: list[str] = []
                tool_rounds += 1
                for call in fn_calls:
                    fn_name = call.name or ""
                    fn_args = dict(call.args or {})
                    self.step_callback(f"[Step {steps}/{self.config.max_steps}] Running {fn_name}...")
                    res = self.registry.execute(fn_name, fn_args, user_input, dry_run, self._preview(fn_name, fn_args))
                    if res.get("ok", False):
                        last_tool = (fn_name, res)
                    if not res.get("ok", False):
                        if res.get("human_only_instruction"):
                            human_notes.append(str(res["human_only_instruction"]))
                        elif not res.get("denied", False):
                            failures += 1
                    else:
                        failures = 0

                    part = types.Part.from_function_response(
                        name=fn_name,
                        response=self._local_tool_payload(fn_name, res)
                        if self.config.backend == "local"
                        else {"result": res},
                    )
                    if getattr(call, "id", None) and getattr(part, "function_response", None):
                        try:
                            part.function_response.id = call.id
                        except Exception:
                            pass
                    resp_parts.append(part)

                self.history.append(types.Content(role="user", parts=resp_parts))
                if human_notes:
                    return AgentTaskResult(True, "\n".join(human_notes), steps, usage_acc, dry_run=dry_run)
                if failures >= self.config.max_repeated_failures:
                    msg = f"Stopped after {failures} repeated failures. How should I proceed?"
                    return AgentTaskResult(False, msg, steps, usage_acc, msg)

            return AgentTaskResult(False, f"Stopped at {self.config.max_steps}-step safety cap.", steps, usage_acc)
        finally:
            self.watchdog.stop_task()
