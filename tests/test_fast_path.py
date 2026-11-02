"""Fast path: simple whole sentences never reach the LLM; unclear ones always do."""

from __future__ import annotations

from typing import Any

import pytest

from assistant.brain.fast_path import match_fast_command, try_fast_path
from assistant.config import load_config
from assistant.safety.permissions import PermissionGate
from assistant.tools.apps import open_app
from assistant.tools.registry import ToolRegistry, tool


class ExplodingLLM:
    """Stands in for the model: any call means the fast path failed."""

    def run(self, _text: str) -> str:
        raise AssertionError("the LLM must not be called for a fast-path sentence")


def _registry(calls: list[tuple[str, dict[str, Any]]]) -> ToolRegistry:
    @tool(name="open_app", risk_level="safe", read_only=False)
    def fake_open(app_name: str) -> dict[str, Any]:
        """Open an app."""
        calls.append(("open_app", {"app_name": app_name}))
        return {"ok": True, "app_name": app_name}

    @tool(name="close_app", risk_level="safe", read_only=False)
    def fake_close(app_name: str) -> dict[str, Any]:
        """Close an app."""
        calls.append(("close_app", {"app_name": app_name}))
        display = app_name.title()
        return {"ok": True, "app_name": display, "closed": True, "running": True}

    @tool(name="mute_audio", risk_level="safe", read_only=False)
    def fake_mute(muted: bool = True) -> dict[str, Any]:
        """Mute or unmute."""
        calls.append(("mute_audio", {"muted": muted}))
        return {"ok": True, "muted": muted}

    @tool(name="set_volume", risk_level="safe", read_only=False)
    def fake_volume(percent: int = 50) -> dict[str, Any]:
        """Set volume."""
        calls.append(("set_volume", {"percent": percent}))
        return {"ok": True, "requested_percent": percent}

    @tool(name="list_files", risk_level="safe", read_only=True)
    def fake_list(directory: str = ".") -> dict[str, Any]:
        """List files."""
        calls.append(("list_files", {"directory": directory}))
        return {"ok": True, "directory": directory, "count": 3, "items": []}

    @tool(name="web_search", risk_level="safe", read_only=False)
    def fake_search(query: str, browser: str | None = None) -> dict[str, Any]:
        """Search the web."""
        calls.append(("web_search", {"query": query, "browser": browser}))
        return {"ok": True, "query": query, "browser": browser or ""}

    @tool(name="open_first_result", risk_level="safe", read_only=False)
    def fake_first(query: str) -> dict[str, Any]:
        """Open first result."""
        calls.append(("open_first_result", {"query": query}))
        return {"ok": True, "query": query, "domain": "github.com"}

    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)
    for fn in (fake_open, fake_close, fake_mute, fake_volume, fake_list, fake_search, fake_first):
        registry.register(fn)
    return registry


def _answer(registry: ToolRegistry, text: str) -> str:
    return try_fast_path(registry, text) or ExplodingLLM().run(text)


@pytest.mark.parametrize(
    ("sentence", "tool_name", "arguments"),
    [
        ("open firefox", "open_app", {"app_name": "firefox"}),
        ("open fire fox", "open_app", {"app_name": "firefox"}),
        ("open 5ox", "open_app", {"app_name": "firefox"}),
        ("mute", "mute_audio", {"muted": True}),
        ("unmute", "mute_audio", {"muted": False}),
        ("turn volume to thirty percent", "set_volume", {"percent": 30}),
        ("list files in my downloads folder", "list_files", {"directory": "downloads"}),
        ("open firefox and search for github", "web_search", {"query": "github", "browser": "firefox"}),
        ("search github in firefox", "web_search", {"query": "github", "browser": "firefox"}),
        ("search for github", "web_search", {"query": "github", "browser": None}),
        ("open the first result for github", "open_first_result", {"query": "github"}),
        ("open the first link for the GitHub", "open_first_result", {"query": "github"}),
        ("open the first link on the GitHub", "open_first_result", {"query": "github"}),
        ("open first link of github", "open_first_result", {"query": "github"}),
        ("search github and open the first link", "open_first_result", {"query": "github"}),
        ("close firefox", "close_app", {"app_name": "firefox"}),
        ("quit firefox", "close_app", {"app_name": "firefox"}),
        ("can you please close firefox", "close_app", {"app_name": "firefox"}),
        ("close the firefox for me", "close_app", {"app_name": "firefox"}),
        (
            "Fire Fox is open. Can you close the firefox for me?",
            "close_app",
            {"app_name": "firefox"},
        ),
    ],
)
def test_fast_path_runs_the_existing_tool_without_the_llm(
    sentence: str, tool_name: str, arguments: dict[str, Any]
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    registry = _registry(calls)

    reply = _answer(registry, sentence)

    assert calls == [(tool_name, arguments)]
    assert reply


def test_open_first_link_never_calls_open_app() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    registry = _registry(calls)

    assert try_fast_path(registry, "Open the first link for the GitHub.") == (
        "Opening the first result for github."
    )
    assert calls == [("open_first_result", {"query": "github"})]


def test_unknown_app_is_never_claimed_open() -> None:
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(open_app)

    reply = try_fast_path(registry, "Open the banana spaceship app")

    assert reply == "I couldn't find an app named banana spaceship app."
    assert "is open" not in reply.lower()


def test_close_reply_is_built_from_the_real_result() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    registry = _registry(calls)

    assert try_fast_path(registry, "close firefox") == "Closed Firefox."


def test_an_unclear_sentence_goes_to_the_llm() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    registry = _registry(calls)

    assert match_fast_command("what is the weather today") is None
    assert try_fast_path(registry, "what is the weather today") is None
    with pytest.raises(AssertionError, match="LLM"):
        _answer(registry, "what is the weather today")
    assert calls == []


def test_compound_sentences_are_split_but_an_incomplete_one_is_not() -> None:
    compound = match_fast_command("open firefox and list my downloads")
    assert compound is not None and compound["kind"] == "compound"
    assert [c["tool"] for c in compound["commands"]] == ["open_app", "list_files"]
    assert match_fast_command("turn the volume up")["tool"] == "volume_up"
    assert match_fast_command("set the volume") is None


def test_replies_are_built_in_code() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    registry = _registry(calls)

    assert try_fast_path(registry, "open firefox") == "Firefox is open."
    assert try_fast_path(registry, "mute") == "Audio is now muted."
    assert try_fast_path(registry, "unmute") == "Audio is now unmuted."
    assert try_fast_path(registry, "turn volume to thirty percent") == "Volume is now set to 30 percent."
    assert try_fast_path(registry, "list files in my downloads folder") == (
        "There are 3 items in your downloads folder."
    )
    assert try_fast_path(registry, "open firefox and search for github") == "Searching for github in Firefox."
    assert try_fast_path(registry, "open the first result for github") == (
        "Opening the first result for github."
    )
