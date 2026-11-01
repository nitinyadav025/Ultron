"""Agent Loop: plan -> act -> observe -> verify -> report.
Enforces 25-step cap, wall-clock cap, 3-failure stop, parallel tool calls, and live redirection.
"""

from __future__ import annotations

import queue
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from google.genai import types

from assistant.brain.gemini_client import GeminiAdapter, TokenUsageStats
from assistant.brain.local_client import LocalAdapter
from assistant.brain.prompts import build_chat_prompt, build_system_prompt
from assistant.brain.reply_style import short_plain_reply
from assistant.brain.tool_output import compact_tool_result
from assistant.brain.tool_replies import (
    FOLDER_NOT_FOUND_REPLY,
    claims_an_empty_folder,
    code_reply_for_tool,
    fallback_reply,
    is_lead_in,
    mute_audio_reply,
)
from assistant.config import AssistantConfig
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.redact import redact_secrets
from assistant.tools.apps import resolve_app_launch_command
from assistant.tools.registry import ToolRegistry
from assistant.tools.volume import (
    TIMED_MUTE_REPLY,
    WHAT_MUTE_REPLY,
    WHAT_VOLUME_REPLY,
    asks_for_timed_mute,
    mute_intent,
    percent_is_mentioned,
)

# Openers that announce an answer instead of giving it.
_AFFIRMATIVE = {
    "yes",
    "yes please",
    "yeah",
    "yep",
    "sure",
    "ok",
    "okay",
    "do it",
    "go ahead",
    "please do",
    "mute it",
    "please mute",
}


_APP_ACTION = re.compile(r"\b(?:open|launch|close|quit|exit|kill)\b", re.I)


def is_affirmative(text: str) -> bool:
    """True for a short yes-like answer to the timed-mute question."""
    cleaned = re.sub(r"[^a-z ]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip() in _AFFIRMATIVE


def request_wants_app_action(text: str) -> bool:
    """True only when the user actually asked to open/launch/close/quit an app."""
    return bool(_APP_ACTION.search(text or ""))


# Words that mean "this needs a tool". Anything without one is pure conversation and
# can use the lean chat request (no ~500-token tool schema).
_TOOL_INTENT = re.compile(
    r"\b(?:open|launch|close|quit|exit|switch|focus|search|list|read|write|type|"
    r"scroll|tab|tabs|window|windows|workspace|brightness|media|play|pause|song|"
    r"track|battery|screenshot|capture|volume|mute|unmute|files?|folders?|downloads?|"
    r"first link|first result|status)\b",
    re.I,
)


def looks_like_pure_chat(text: str) -> bool:
    """True for a conversational turn that cannot need a tool."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    return not bool(_TOOL_INTENT.search(cleaned))


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
        self._pending_mute_offer = False

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
        # "There are no items..." is only allowed when code itself built it from a real,
        # empty listing (handled above). A model worded claim here is never backed by data.
        if claims_an_empty_folder(shaped):
            return FOLDER_NOT_FOUND_REPLY
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
        request_text = user_input.strip()
        cleaned = request_text
        if cleaned.lower().startswith("plan only"):
            dry_run = True
            cleaned = cleaned[9:].lstrip(": ").strip() or user_input
        if dry_run:
            cleaned = f"[DRY RUN / PLAN ONLY] Print the step-by-step plan without executing tools for: {cleaned}"

        self.watchdog.start_task()
        self.registry.gate.tainted_by_untrusted = False
        usage_acc = TokenUsageStats(daily_total=self.adapter.usage_meter.get_today_total(), daily_cap=self.config.daily_token_cap)
        self.history.append(types.Content(role="user", parts=[types.Part.from_text(text=redact_secrets(cleaned))]))

        answered_mute_offer = self._pending_mute_offer
        self._pending_mute_offer = False
        if asks_for_timed_mute(request_text):
            # No timer exists: ask first and run nothing (the next "yes" may mute now).
            self._pending_mute_offer = True
            self.watchdog.stop_task()
            self.step_callback("Timed mute requested; no timer available.")
            return AgentTaskResult(
                ok=True, reply=TIMED_MUTE_REPLY, steps_taken=0, usage=usage_acc, dry_run=dry_run
            )
        if answered_mute_offer and not dry_run and is_affirmative(request_text):
            # The user accepted "Should I mute it now?": do it directly, no model guess.
            res = self.registry.execute("mute_audio", {"muted": True}, request_text)
            self.watchdog.stop_task()
            return AgentTaskResult(
                ok=True,
                reply=mute_audio_reply(res) or "I couldn't change the audio.",
                steps_taken=0,
                usage=usage_acc,
            )

        if self.config.backend == "local" and looks_like_pure_chat(cleaned):
            # Pure conversation: send no tool schemas and only the last four turns.
            self.step_callback("Thinking (chat)...")
            history = self.history[-8:]
            resp, turn_u = self.adapter.generate_turn(
                contents=history,
                system_instruction=build_chat_prompt(self.config),
                tools=None,
                max_output_tokens=LOCAL_MAX_TOKENS_FOLLOWUP,
            )
            usage_acc.prompt_tokens += turn_u.prompt_tokens
            usage_acc.candidate_tokens += turn_u.candidate_tokens
            usage_acc.thought_tokens += turn_u.thought_tokens
            usage_acc.total_tokens += turn_u.total_tokens
            usage_acc.daily_total = turn_u.daily_total
            self.watchdog.stop_task()
            if not resp.candidates:
                return AgentTaskResult(False, "No response candidates returned.", 1, usage_acc)
            reply = self._finalize_reply(resp.text or "I'm not sure how to answer that.")
            self.history.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
            return AgentTaskResult(True, reply, 1, usage_acc, dry_run=dry_run)

        failures = 0
        steps = 0
        tool_rounds = 0
        last_tool: tuple[str, dict[str, Any]] | None = None
        local_tool_round_cap = 3 if self.config.backend == "local" else None
        # Set when the model proposed an app action nobody asked for: answer without tools.
        suppress_tools = False
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

                if dry_run or suppress_tools:
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

                # Never guess an action the user did not say: run no tool and ask instead.
                mute_overrides: dict[int, bool] = {}
                unrequested_app_calls: set[int] = set()
                for index, call in enumerate(fn_calls):
                    call_name = call.name or ""
                    if call_name in ("open_app", "close_app") and not request_wants_app_action(request_text):
                        unrequested_app_calls.add(index)
                        continue
                    if call_name == "set_volume":
                        wanted = dict(call.args or {}).get("percent", 50)
                        if not percent_is_mentioned(wanted, request_text):
                            self.step_callback("set_volume without a spoken number; asking.")
                            return AgentTaskResult(True, WHAT_VOLUME_REPLY, steps, usage_acc, dry_run=dry_run)
                    elif call_name == "mute_audio":
                        intent = mute_intent(request_text)
                        if intent is None and answered_mute_offer and is_affirmative(request_text):
                            intent = "mute"
                        if intent is None:
                            self.step_callback("mute_audio without a clear mute/unmute word; asking.")
                            return AgentTaskResult(True, WHAT_MUTE_REPLY, steps, usage_acc, dry_run=dry_run)
                        # Force the action the user actually said, whatever the model chose.
                        mute_overrides[index] = intent == "mute"

                resp_parts: list[types.Part] = []
                human_notes: list[str] = []
                tool_rounds += 1
                for call_index, call in enumerate(fn_calls):
                    fn_name = call.name or ""
                    fn_args = dict(call.args or {})
                    if call_index in unrequested_app_calls:
                        # The user asked a question, not for an app action. Refuse the call
                        # and force a plain answer on the next turn (no tools).
                        suppress_tools = True
                        self.step_callback(f"No app action requested; skipping {fn_name}.")
                        part = types.Part.from_function_response(
                            name=fn_name,
                            response={"ok": False, "error": "No app action was requested."},
                        )
                        if getattr(call, "id", None) and getattr(part, "function_response", None):
                            try:
                                part.function_response.id = call.id
                            except Exception:
                                pass
                        resp_parts.append(part)
                        continue
                    if call_index in mute_overrides:
                        fn_args["muted"] = mute_overrides[call_index]
                    self.step_callback(f"[Step {steps}/{self.config.max_steps}] Running {fn_name}...")
                    res = self.registry.execute(fn_name, fn_args, user_input, dry_run, self._preview(fn_name, fn_args))
                    # Remember every call, including failures: a failed list must never be
                    # answered with the model's invented "no items" sentence.
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
