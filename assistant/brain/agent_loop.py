"""Agent Loop: plan -> act -> observe -> verify -> report.
Enforces 25-step cap, wall-clock cap, 3-failure stop, parallel tool calls, and live redirection.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from google.genai import types

from assistant.brain.context import SESSION, context_tag, get_screen_context
from assistant.brain.gemini_client import GeminiAdapter, TokenUsageStats
from assistant.brain.local_client import LocalAdapter
from assistant.brain.prompts import build_chat_prompt, build_system_prompt
from assistant.brain.reply_style import clean_llm_reply, short_plain_reply
from assistant.brain.skills import (
    match_skill,
    parse_memory_teaching,
    parse_teaching_command,
    prompt_hint,
    save_user_alias,
    save_user_fact,
    save_user_skill,
)
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


# Phase 7: 220 tokens so a type_text paragraph and a 1-2 sentence spoken reply are never
# chopped mid-sentence ("...goals on." / "...use for."). The sentence guard in
# reply_style.clean_llm_reply trims a reply that still hits the cap.
LOCAL_MAX_TOKENS_FIRST = 220
LOCAL_MAX_TOKENS_FOLLOWUP = 220


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

    HISTORY_MAX_MESSAGES = 6
    HISTORY_MAX_TURNS = 3

    @staticmethod
    def _text_only(content: types.Content) -> types.Content | None:
        texts = [
            part.text
            for part in (getattr(content, "parts", None) or [])
            if getattr(part, "text", None)
        ]
        if not texts:
            return None
        return types.Content(
            role=getattr(content, "role", None) or "user",
            parts=[types.Part.from_text(text=" ".join(texts).strip())],
        )

    def _prune_history(
        self, max_messages: int | None = None, *, keep_current_protocol: bool = False
    ) -> None:
        """Keep at most three text-only turns (user + spoken reply).

        At the start of a turn every old tool-call JSON blob is dropped. At the end of a
        turn the current turn's tool protocol may stay (still capped at 6 messages), so the
        loop can finish its own work and tests can inspect the payload it sent.
        """
        limit = max(2, int(max_messages or self.HISTORY_MAX_MESSAGES))
        if not keep_current_protocol:
            kept = [item for item in (self._text_only(c) for c in self.history) if item]
            self.history = kept[-limit:]
            return

        last_user = -1
        for index, content in enumerate(self.history):
            if getattr(content, "role", None) != "user":
                continue
            if any(getattr(part, "text", None) for part in getattr(content, "parts", None) or []):
                last_user = index
        if last_user < 0:
            kept = [item for item in (self._text_only(c) for c in self.history) if item]
            self.history = kept[-limit:]
            return
        prior = [
            item
            for item in (self._text_only(c) for c in self.history[:last_user])
            if item
        ]
        tail = list(self.history[last_user:])
        if len(tail) > limit:
            tail = [item for item in (self._text_only(c) for c in tail[-limit:]) if item]
        room = max(0, limit - len(tail))
        self.history = prior[-room:] + tail if room else tail

    @staticmethod
    def _tagged_contents(
        contents: list[types.Content], tag: str
    ) -> list[types.Content]:
        """Copy the history and prepend one context tag to the current user text.

        The stored history stays 100% clean (no tag, no screen text), so the system
        prompt and the cached prefix never change. Only the payload sent to the model is
        decorated.
        """
        copied = list(contents)
        for index in range(len(copied) - 1, -1, -1):
            content = copied[index]
            if getattr(content, "role", None) != "user":
                continue
            parts = list(getattr(content, "parts", None) or [])
            text_indexes = [
                part_index
                for part_index, part in enumerate(parts)
                if getattr(part, "text", None)
            ]
            if not text_indexes:
                continue
            tagged_parts: list[types.Part] = []
            for part_index, part in enumerate(parts):
                if part_index == text_indexes[-1]:
                    tagged_parts.append(
                        types.Part.from_text(text=f"{tag}\n{getattr(part, 'text', '')}")
                    )
                else:
                    tagged_parts.append(part)
            copied[index] = types.Content(role="user", parts=tagged_parts)
            break
        return copied

    def record_external_turn(
        self,
        user_text: str,
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        reply: str = "",
    ) -> None:
        """Record a fast-path/learned-skill turn in the same history as the LLM turns.

        Follow-ups such as "write what is an AI agent inside this new document" or
        "what did you say?" then see the real previous action and answer. SESSION keeps
        the last spoken reply for an instant repeat.
        """
        cleaned_user = redact_secrets((user_text or "").strip())
        cleaned_reply = (reply or "").strip()
        if cleaned_user:
            self.history.append(
                types.Content(role="user", parts=[types.Part.from_text(text=cleaned_user)])
            )
        if cleaned_reply:
            self.history.append(
                types.Content(role="model", parts=[types.Part.from_text(text=cleaned_reply)])
            )
            SESSION.last_reply = cleaned_reply
        if tool_name:
            SESSION.note_action(
                f"{tool_name} {json.dumps(tool_args or {}, ensure_ascii=False, default=str)}"
            )
        self._prune_history()

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
        shaped = short_plain_reply(
            clean_llm_reply(text), max_chars=self.config.voice_reply_max_chars
        )
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

    def _run_skill(self, skill: Any) -> list[str]:
        """Execute a learned action chain through the deterministic fast path."""
        from assistant.brain.fast_path import match_fast_command, run_fast_command

        replies: list[str] = []
        for action in getattr(skill, "actions", ()) or ():
            command = match_fast_command(str(action))
            if command is None:
                self.step_callback(f"Skill action needs the model: {action}")
                continue
            reply = run_fast_command(self.registry, command)
            if reply and reply not in replies:
                replies.append(reply)
        if replies:
            SESSION.note_action(f"skill {getattr(skill, 'trigger', '')}".strip())
            SESSION.last_reply = " ".join(replies)
        return replies

    def run_task(self, user_input: str, dry_run: bool = False) -> AgentTaskResult:
        """Public entry point: prune before and after so no turn can grow the transcript."""
        # Leave one slot for the new user message: the prompt it produces stays at 6.
        self._prune_history(self.HISTORY_MAX_MESSAGES - 1)
        try:
            result = self._run_task(user_input, dry_run)
        finally:
            self._prune_history(keep_current_protocol=True)
        if result.reply:
            SESSION.last_reply = result.reply
        return result

    def _run_task(self, user_input: str, dry_run: bool = False) -> AgentTaskResult:
        request_text = user_input.strip()
        live_screen = get_screen_context()
        skills_hint = prompt_hint(request_text, self.config)
        turn_context = context_tag(live_screen, skills_hint)
        # Static for every call in this task: screen/skills never enter the system message.
        gate_mode = getattr(self.registry.gate, "mode", "auto-safe")
        local_system_prompt = build_system_prompt(self.config, gate_mode)
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

        teaching = parse_teaching_command(request_text)
        if teaching and not dry_run:
            trigger, actions = teaching
            try:
                skill = save_user_skill(self.config, trigger, actions)
            except (OSError, ValueError) as exc:
                reply = f"I couldn't learn that skill: {exc}"
            else:
                reply = (
                    f"I've learned that when you say {skill.trigger}, I should "
                    + " and ".join(skill.actions)
                    + "."
                )
            self.history.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
            self.watchdog.stop_task()
            return AgentTaskResult(True, reply, 0, usage_acc, dry_run=dry_run)

        memory_teaching = parse_memory_teaching(request_text)
        if memory_teaching and not dry_run:
            kind, name, value = memory_teaching
            try:
                if kind == "alias":
                    alias, target = save_user_alias(self.config, name, value)
                    reply = f"Got it. When you say {alias}, I'll use {target}."
                else:
                    key = save_user_fact(self.config, name, value)
                    reply = f"Got it. I've saved your {key.replace('_', ' ')}."
            except (OSError, ValueError) as exc:
                reply = f"I couldn't save that: {exc}"
            self.history.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
            self.watchdog.stop_task()
            return AgentTaskResult(True, reply, 0, usage_acc, dry_run=dry_run)

        matched_skill = match_skill(request_text, self.config)
        if matched_skill is not None and not dry_run:
            replies = self._run_skill(matched_skill)
            if replies:
                combined = " ".join(replies)
                self.history.append(
                    types.Content(role="model", parts=[types.Part.from_text(text=combined)])
                )
                self.watchdog.stop_task()
                return AgentTaskResult(True, combined, 0, usage_acc, dry_run=dry_run)

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
                contents=self._tagged_contents(history, turn_context),
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
                    tools_payload = self.registry.get_openai_tools(compact=True)
                else:
                    tools_payload = self.registry.get_gemini_tools()
                resp, turn_u = self.adapter.generate_turn(
                    contents=self._tagged_contents(self.history, turn_context),
                    system_instruction=local_system_prompt,
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
                executed_tools: list[tuple[str, dict[str, Any]]] = []
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
                    executed_tools.append((fn_name, res))
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

                if self.config.backend == "local" and executed_tools:
                    # tool_replies.py already knows the exact sentence for these tools. The
                    # old loop made a SECOND llama-server call only to overwrite that answer
                    # with the same code-built reply (7.38 s in the Nov 2 log). Return now.
                    built: list[str] = []
                    for tool_name, tool_result in executed_tools:
                        reply = code_reply_for_tool(tool_name, tool_result)
                        if not reply:
                            built = []
                            break
                        if reply not in built:
                            built.append(reply)
                    if built:
                        combined = " ".join(built)
                        self.history.append(
                            types.Content(role="model", parts=[types.Part.from_text(text=combined)])
                        )
                        self.step_callback("Done — deterministic tool reply; no second LLM call.")
                        return AgentTaskResult(True, combined, steps, usage_acc, dry_run=dry_run)

            return AgentTaskResult(False, f"Stopped at {self.config.max_steps}-step safety cap.", steps, usage_acc)
        finally:
            self.watchdog.stop_task()
