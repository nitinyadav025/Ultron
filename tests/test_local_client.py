"""Offline tests for the local llama.cpp adapter (no network, no model, no server)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from google.genai import types

from assistant.brain.local_client import LocalAdapter, translate_history
from assistant.config import load_config


def _adapter(handler: Any) -> LocalAdapter:
    cfg = load_config()
    cfg.backend = "local"
    return LocalAdapter(cfg, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _completion(message: dict[str, Any], usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {"choices": [{"message": message}], "usage": usage or {}}


def _user_turn(text: str) -> list[types.Content]:
    return [types.Content(role="user", parts=[types.Part.from_text(text=text)])]


def test_final_text_reply_and_sampling_params() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_completion(
                {"role": "assistant", "content": "Hello there."},
                usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            ),
        )

    adapter = _adapter(handler)
    resp, stats = adapter.generate_turn(_user_turn("hi"), "You are Ultron.")
    assert resp.text == "Hello there."
    assert not resp.function_calls
    body = seen[0]
    assert body["messages"][0] == {"role": "system", "content": "You are Ultron."}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["temperature"] == 0.1
    assert body["top_k"] == 50
    assert body["repeat_penalty"] == 1.15
    assert body["stop"] == ["<|im_end|>", "<|im_start|>", "<|endoftext|>", "<|startoftext|>"]
    assert body["stream"] is False
    assert stats.total_tokens == 14
    assert stats.daily_cap == 0  # no Gemini-style token cap on the local backend


def test_special_tokens_and_repeated_sentences_are_cleaned() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": "I am Ultron.< name is Ultron, the voice assistant. Done. Done.",
                }
            ),
        )

    adapter = _adapter(handler)
    resp, _ = adapter.generate_turn(_user_turn("what is your name?"), "sys")

    assert "<" not in resp.text
    assert "Done. Done." not in resp.text
    assert resp.text.startswith("I am Ultron. name is Ultron")
    assert resp.text.endswith("Done.")


def test_tool_call_parsing_round_trip() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "open_app", "arguments": '{"app_name": "brave"}'},
                        }
                    ],
                }
            ),
        )

    adapter = _adapter(handler)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "open_app",
                "description": "Launch an app",
                "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]},
            },
        }
    ]
    resp, _ = adapter.generate_turn(_user_turn("open brave"), "sys", tools=tools)
    calls = resp.function_calls
    assert len(calls) == 1
    assert calls[0].name == "open_app"
    assert calls[0].args == {"app_name": "brave"}
    assert calls[0].id == "call_abc"

    # The model Content must translate back into an assistant tool_calls message.
    msgs = translate_history([resp.candidates[0].content])
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["id"] == "call_abc"
    assert json.loads(msgs[0]["tool_calls"][0]["function"]["arguments"]) == {"app_name": "brave"}


def test_malformed_tool_json_retries_once() -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] == 1:
            return httpx.Response(
                200,
                json=_completion(
                    {
                        "content": "",
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "volume_up", "arguments": "{bad json"}}
                        ],
                    }
                ),
            )
        body = json.loads(request.content)
        assert body["messages"][-1]["role"] == "tool"
        assert "valid JSON" in body["messages"][-1]["content"]
        assert body["messages"][-1]["tool_call_id"] == "call_1"
        return httpx.Response(
            200,
            json=_completion(
                {
                    "content": "",
                    "tool_calls": [
                        {"id": "call_2", "type": "function", "function": {"name": "volume_up", "arguments": '{"step_percent": 10}'}}
                    ],
                }
            ),
        )

    adapter = _adapter(handler)
    resp, _ = adapter.generate_turn(_user_turn("louder"), "sys")
    assert counter["n"] == 2
    assert resp.function_calls[0].args == {"step_percent": 10}


def test_malformed_tool_json_twice_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "volume_up", "arguments": "not-json"}}
                    ],
                }
            ),
        )

    adapter = _adapter(handler)
    with pytest.raises(RuntimeError, match="malformed tool-call JSON twice"):
        adapter.generate_turn(_user_turn("louder"), "sys")


def test_server_down_clear_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = _adapter(handler)
    with pytest.raises(RuntimeError, match="Cannot reach the local LLM server"):
        adapter.generate_turn(_user_turn("hello"), "sys")


def test_history_translation_with_tool_response() -> None:
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text="open brave")]),
        types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="open_app", args={"app_name": "brave"}, id="call_9"))],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="open_app", response={"result": {"ok": True}}, id="call_9"
                    )
                )
            ],
        ),
    ]
    messages = translate_history(contents)
    assert messages[0] == {"role": "user", "content": "open brave"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["id"] == "call_9"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_9"
    assert json.loads(messages[2]["content"])["result"]["ok"] is True
