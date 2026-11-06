"""Local LLM adapter: OpenAI-compatible client for llama.cpp llama-server (LFM3-1.7B).

Drop-in replacement for GeminiAdapter: same methods (select_model, generate_turn) and a
response object shaped like the google-genai GenerateContentResponse, so AgentLoop works
unchanged. Tool calls arrive in choices[0].message.tool_calls with arguments as a JSON
string; they are converted back into google-genai types (Content/Part/FunctionCall).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.reply_style import clean_llm_reply
from assistant.config import AssistantConfig
from assistant.genai_types import types
from assistant.safety.redact import redact_secrets

logger = logging.getLogger("ultron.local")

JSON_CORRECTION_PROMPT = (
    "ERROR: the previous tool call arguments were not valid JSON. "
    "Repeat the same tool call with arguments as a valid JSON object string."
)

# LFM3's chat template (confirmed in the GGUF): stop before any control token leaks
# into the visible answer. llama-server itself keeps its launch flags unchanged.
LOCAL_STOP_SEQUENCES = ("<|im_end|>", "<|im_start|>", "<|endoftext|>", "<|startoftext|>")
REPEAT_PENALTY = 1.15


class _MalformedToolJSON(Exception):
    """Raised internally when the model emits a tool call with unparseable arguments."""

    def __init__(self, raw_message: dict[str, Any]) -> None:
        super().__init__("malformed tool-call arguments")
        self.raw_message = raw_message


@dataclass
class LocalUsageMeter:
    """Token-cap stub for the local backend (no daily quota is enforced on-device)."""

    def get_today_total(self) -> int:
        return 0


def translate_history(contents: list[types.Content]) -> list[dict[str, Any]]:
    """Convert a google-genai Content history into OpenAI chat messages (incl. tool calls)."""
    messages: list[dict[str, Any]] = []
    for content in contents:
        role = "assistant" if getattr(content, "role", None) == "model" else "user"
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_messages: list[dict[str, Any]] = []

        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                text_parts.append(text)

            fn_call = getattr(part, "function_call", None)
            if fn_call is not None and getattr(fn_call, "name", None):
                call_id = str(getattr(fn_call, "id", None) or f"call_{len(messages)}_{len(tool_calls)}")
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": fn_call.name,
                            "arguments": json.dumps(dict(fn_call.args or {}), ensure_ascii=False),
                        },
                    }
                )

            fn_resp = getattr(part, "function_response", None)
            if fn_resp is not None and getattr(fn_resp, "name", None):
                response = getattr(fn_resp, "response", None)
                if not isinstance(response, dict):
                    response = {"result": response}
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(getattr(fn_resp, "id", None) or ""),
                        "content": json.dumps(response, ensure_ascii=False, default=str),
                    }
                )

        text = "".join(text_parts).strip()
        if role == "assistant":
            if tool_calls:
                messages.append({"role": "assistant", "content": text or None, "tool_calls": tool_calls})
            elif text:
                messages.append({"role": "assistant", "content": text})
        elif tool_messages:
            messages.extend(tool_messages)
        elif text:
            messages.append({"role": "user", "content": text})

    return messages


class LocalAdapter:
    """Adapter around a local llama.cpp server exposing the OpenAI chat-completions API."""

    is_local = True

    def __init__(self, config: AssistantConfig, http_client: httpx.Client | None = None) -> None:
        self.config = config
        self.usage_meter = LocalUsageMeter()
        self._owns_http = http_client is None
        timeout = httpx.Timeout(
            connect=3.0,
            read=float(config.llama_timeout_sec),
            write=10.0,
            pool=5.0,
        )
        self._http = http_client or httpx.Client(timeout=timeout)

    def select_model(self, role: str = "main") -> str:
        """Every role maps to the single local model."""
        return self.config.local_model_name

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # -- HTTP -----------------------------------------------------------------

    def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.llama_base_url}/chat/completions"
        try:
            resp = self._http.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Local LLM server timed out after {self.config.llama_timeout_sec:.0f}s at {url}."
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Cannot reach the local LLM server at {self.config.llama_base_url} "
                f"({type(exc).__name__}). Ultron starts llama-server automatically on startup; "
                f"if this persists, check {self.config.llama_server_log}."
            ) from exc
        if resp.status_code != 200:
            detail = (resp.text or "").strip()[:300]
            raise RuntimeError(f"Local LLM server returned HTTP {resp.status_code}: {detail}")
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError("Local LLM server returned a non-JSON response.") from exc

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int = 220,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.select_model(),
            "messages": messages,
            "temperature": 0.1,
            "top_k": 50,
            "repeat_penalty": REPEAT_PENALTY,
            "stop": list(LOCAL_STOP_SEQUENCES),
            "stream": False,
            "max_tokens": max_tokens,
            # Pin the request to slot 0 and reuse the cached static prompt prefix. The
            # system message never changes, so llama-server keeps it in VRAM.
            "id_slot": 0,
            "cache_prompt": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    # -- Response parsing -----------------------------------------------------

    @staticmethod
    def _extract(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Local LLM server returned an unexpected response shape.") from exc

        text = clean_llm_reply(str(message.get("content") or "").strip())
        usage = data.get("usage") or {}
        calls: list[dict[str, Any]] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            fn = raw_call.get("function") or {}
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args or "{}")
                except (TypeError, ValueError) as exc:
                    raise _MalformedToolJSON(message) from exc
                if not isinstance(args, dict):
                    raise _MalformedToolJSON(message)
            calls.append({"id": str(raw_call.get("id") or f"call_{index}"), "name": name, "args": args})
        return text, calls, usage

    @staticmethod
    def _first_tool_call_id(raw_message: dict[str, Any]) -> str:
        for raw_call in raw_message.get("tool_calls") or []:
            call_id = raw_call.get("id")
            if call_id:
                return str(call_id)
        return "call_0"

    @staticmethod
    def _build_response(text: str, calls: list[dict[str, Any]]) -> types.GenerateContentResponse:
        parts: list[types.Part] = []
        for call in calls:
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(name=call["name"], args=call["args"], id=call["id"])
                )
            )
        parts.append(types.Part.from_text(text=text or ""))
        content = types.Content(role="model", parts=parts)
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)])

    # -- Public API (mirrors GeminiAdapter) -------------------------------------

    def generate_turn(
        self,
        contents: list[types.Content],
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        """Run one chat-completions turn against llama-server and return an SDK-shaped response."""
        max_tokens = int(max_output_tokens or 220)
        messages = [{"role": "system", "content": redact_secrets(system_instruction)}]
        messages.extend(translate_history(contents))

        data = self._post_chat(self._payload(messages, tools, max_tokens))
        try:
            text, calls, usage = self._extract(data)
        except _MalformedToolJSON as bad:
            logger.warning("Malformed tool-call JSON from local model; retrying once with a correction.")
            retry_messages = messages + [
                {
                    "role": "assistant",
                    "content": str(bad.raw_message.get("content") or ""),
                    "tool_calls": bad.raw_message.get("tool_calls") or [],
                },
                {
                    "role": "tool",
                    "tool_call_id": self._first_tool_call_id(bad.raw_message),
                    "content": JSON_CORRECTION_PROMPT,
                },
            ]
            data = self._post_chat(self._payload(retry_messages, tools, max_tokens))
            try:
                text, calls, usage = self._extract(data)
            except _MalformedToolJSON as exc:
                raise RuntimeError("Local model returned malformed tool-call JSON twice.") from exc

        stats = TokenUsageStats(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            candidate_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            daily_total=0,
            daily_cap=0,
        )
        logger.info(
            "Local turn via %s: %d tool call(s), reply_chars=%d",
            self.select_model(role),
            len(calls),
            len(text),
        )
        return self._build_response(text, calls), stats
