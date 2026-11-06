"""Phase 7: Wayland screen sensor, static KV prefix, typing, timers, Flatpak safety.

Everything here is deterministic. No microphone, speaker, browser, keyboard, GUI, network
or Flatpak sandbox is touched: every OS runner is injected, and every command that would
type or paste is mocked.
"""

from __future__ import annotations

import array
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import types as genai_types

import assistant.brain.agent_loop as agent_loop_module
from assistant.brain import context as context_module
from assistant.brain.agent_loop import AgentLoop
from assistant.brain.context import (
    SESSION,
    context_tag,
    ensure_wayland_introspect,
    parse_introspect_windows,
    refresh_screen_context,
)
from assistant.brain.fast_path import match_fast_command, try_fast_path
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalAdapter, LocalUsageMeter
from assistant.brain.prompts import build_system_prompt
from assistant.brain.reply_style import clean_llm_reply
from assistant.brain.skills import (
    configure_runtime,
    export_chatml_dataset,
    memory_value,
    save_user_alias,
    save_user_fact,
    save_user_skill,
)
from assistant.brain.tool_replies import close_app_reply
from assistant.config import PROJECT_ROOT, load_config
from assistant.environment.state import get_state
from assistant.interfaces.listen import (
    HandsFreeListener,
    Transcript,
    is_stop_command,
    normalise_stop_command,
)
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.speech.recorder import FRAME_BYTES, FRAME_SAMPLES
from assistant.speech.wake import normalise_spoken_command
from assistant.tools import apps, desktop_control as dc, web
from assistant.tools.registry import ToolRegistry

FRAME = FRAME_SAMPLES


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    SESSION.reset()
    dc.set_terminal_context(False)
    dc._skip_focus_once = False
    monkeypatch.setattr(dc, "_focus_delay_s", 0.0)
    monkeypatch.setattr(dc, "_navigate_tab_delay_s", 0.0)
    monkeypatch.setattr(dc, "_navigate_paste_delay_s", 0.0)
    monkeypatch.setattr(dc, "_clipboard_paste_delay_s", 0.0)
    yield
    SESSION.reset()
    dc.set_terminal_context(False)
    dc._skip_focus_once = False


def _cfg(tmp_path: Path):
    cfg = load_config()
    cfg.backend = "local"
    cfg.learned_skills_file = tmp_path / "config_skills.json"
    cfg.user_skills_file = tmp_path / "user_skills.json"
    cfg.user_memory_file = tmp_path / "user_memory.json"
    cfg.training_dataset_file = tmp_path / "training_dataset.jsonl"
    cfg.learned_skills_file.write_text('{"version": 1, "skills": []}\n', encoding="utf-8")
    configure_runtime(cfg)
    return cfg


def _done(argv: list[str], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class RecorderRegistry:
    """Registry-shaped fake that records calls and never touches the OS."""

    def __init__(self, responder: Any | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gate = SimpleNamespace(tainted_by_untrusted=False, mode="auto-safe")
        self.needs_confirmation_seen = False
        self.allow_prompts = True
        self.on_execute = None
        self._responder = responder

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_request: str = "",
        dry_run: bool = False,
        action_preview: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, dict(arguments)))
        if self._responder is not None:
            return self._responder(tool_name, arguments)
        if tool_name == "type_text":
            return {"ok": True, "raw_summary": {"ok": True, "reply": "I've written that into your document."}}
        if tool_name == "web_search":
            query = str(arguments.get("query") or "")
            site = str(arguments.get("site") or "")
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "query": query,
                    "site": site,
                    "browser": arguments.get("browser") or "firefox",
                    "url": web.site_search_url(site, query) if site else web.search_url(query),
                },
            }
        if tool_name == "set_timer":
            seconds = int(arguments.get("seconds") or 0)
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "seconds": seconds,
                    "reply": f"I've set a timer for {dc.format_duration(seconds)}.",
                },
            }
        if tool_name == "system_info":
            return {"ok": True, "raw_summary": {"ok": True, "reply": "Today is Monday."}}
        if tool_name == "take_screenshot":
            return {"ok": True, "raw_summary": {"ok": True, "path": "/tmp/shot.png"}}
        return {"ok": True, "raw_summary": {"ok": True}}

    def get_openai_tools(self, compact: bool = False) -> list[dict[str, Any]]:
        return []


class CaptureAdapter:
    """Returns one plain sentence and records the exact payload the loop sent."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, reply: str = "Okay.", tool_call: tuple[str, dict[str, Any]] | None = None) -> None:
        self.reply = reply
        self.tool_call = tool_call
        self.seen: list[dict[str, Any]] = []

    def select_model(self, role: str = "main") -> str:
        return "phase7-stub"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[genai_types.GenerateContentResponse, TokenUsageStats]:
        messages = list(contents)  # type: ignore[arg-type]
        text = "\n".join(
            getattr(part, "text", None) or ""
            for content in messages
            for part in (getattr(content, "parts", None) or [])
        )
        self.seen.append(
            {
                "system": system_instruction,
                "text": text,
                "messages": len(messages),
                "max_tokens": max_output_tokens,
                "tools": tools,
            }
        )
        parts: list[genai_types.Part] = []
        if self.tool_call is not None and len(self.seen) == 1:
            name, args = self.tool_call
            parts.append(
                genai_types.Part(
                    function_call=genai_types.FunctionCall(name=name, args=args, id="call_1")
                )
            )
        else:
            parts.append(genai_types.Part.from_text(text=self.reply))
        content = genai_types.Content(role="model", parts=parts)
        return (
            genai_types.GenerateContentResponse(candidates=[genai_types.Candidate(content=content)]),
            TokenUsageStats(),
        )


def _loop(cfg: Any, registry: Any, adapter: Any) -> AgentLoop:
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    return AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Part 1: GNOME Wayland screen sensor
# --------------------------------------------------------------------------------------

INTROSPECT_OUTPUT = """([{'app-id': <'firefox'>, 'has-focus': <true>, 'title': <'F1 - YouTube - Mozilla Firefox'>, 'wm-class': <'firefox'>}, \
{'app-id': <'org.gnome.Nautilus'>, 'has-focus': <false>, 'title': <'Downloads'>, 'wm-class': <'org.gnome.Nautilus'>}],)"""


def test_wayland_introspect_is_enabled_quietly_once() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return _done(argv)

    assert ensure_wayland_introspect(runner=runner, session_type="wayland") is True
    assert calls[0][0] == ["gsettings", "set", "org.gnome.shell", "introspect", "true"]
    assert calls[0][1]["timeout"] == 1.0
    assert calls[0][1]["capture_output"] is True
    assert ensure_wayland_introspect(runner=runner, session_type="x11") is False


def test_introspect_parser_reads_title_app_class_and_focus() -> None:
    windows = parse_introspect_windows(INTROSPECT_OUTPUT)

    assert len(windows) == 2
    assert windows[0] == {
        "title": "F1 - YouTube - Mozilla Firefox",
        "app_id": "firefox",
        "wm_class": "firefox",
        "has_focus": True,
    }
    assert windows[1]["has_focus"] is False


def test_refresh_screen_context_uses_native_introspect_and_focus() -> None:
    context = refresh_screen_context(
        runner=lambda argv, **_k: _done(argv, 0, ""),
        introspect_runner=lambda argv, **_k: _done(argv, 0, INTROSPECT_OUTPUT),
        process_runner=lambda argv, **_k: _done(argv, 1, ""),
    )

    assert context["windows"] == ["F1 - YouTube - Mozilla Firefox", "Downloads"]
    assert context["browser"] == "firefox"
    assert context["site"] == "youtube"
    assert context["focused_window"] == "F1 - YouTube - Mozilla Firefox"
    assert context["focused_app"] == "firefox"
    assert SESSION.active_site == "youtube"


def test_screen_context_never_empty_with_session_and_process_fallback() -> None:
    SESSION.note_app("firefox", browser=True)
    SESSION.note_search("f1", "firefox")
    SESSION.active_site = "youtube"
    SESSION.last_site = "youtube"

    context = refresh_screen_context(
        runner=lambda argv, **_k: _done(argv, 0, ""),
        introspect_runner=lambda argv, **_k: _done(argv, 0, ""),
        process_runner=lambda argv, **_k: _done(argv, 0, "4242 /usr/bin/firefox\n"),
    )

    assert context["windows"]
    assert "YouTube" in context["windows"]
    assert context["browser"] == "firefox"
    assert context["site"] == "youtube"
    assert SESSION.focused_window


def test_screen_context_keeps_the_site_when_only_a_browser_tab_is_visible() -> None:
    SESSION.active_site = "youtube"
    SESSION.last_site = "youtube"

    context = refresh_screen_context(
        runner=lambda argv, **_k: _done(argv, 0, "0x1 0 host New Tab - Mozilla Firefox\n"),
        introspect_runner=lambda argv, **_k: _done(argv, 0, ""),
        process_runner=lambda argv, **_k: _done(argv, 1, ""),
    )

    assert context["browser"] == "firefox"
    assert context["site"] == "youtube"


def test_claude_is_detected_as_an_active_site() -> None:
    SESSION.note_screen(["Claude - Mozilla Firefox"])
    assert SESSION.active_site == "claude"


# --------------------------------------------------------------------------------------
# Part 2: static system prompt, KV cache payload, 220 tokens, history
# --------------------------------------------------------------------------------------


def test_system_prompt_is_byte_identical_across_changing_screens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    adapter = CaptureAdapter(reply="I can explain that.")
    loop = _loop(cfg, RecorderRegistry(), adapter)

    screens = iter(
        [
            {"windows": ["YouTube"], "browser": "firefox", "site": "youtube", "focused_window": "YouTube", "focused_app": "firefox", "app": "firefox", "last_action": "opened firefox"},
            {"windows": ["GitHub"], "browser": "brave", "site": "github", "focused_window": "GitHub", "focused_app": "brave", "app": "brave", "last_action": "searched github"},
        ]
    )
    monkeypatch.setattr(agent_loop_module, "get_screen_context", lambda: next(screens))

    loop.run_task("search for a local model")
    loop.run_task("search for a local model")

    prompts = [entry["system"] for entry in adapter.seen]
    assert len(set(prompts)) == 1
    assert "Screen:" not in prompts[0]
    assert "Skills:" not in prompts[0]
    assert "browser=firefox" in adapter.seen[0]["text"]
    assert "browser=brave" in adapter.seen[1]["text"]
    # The stored history is clean: the tag never survives into the next prompt.
    history_text = "\n".join(
        getattr(part, "text", None) or ""
        for content in loop.history
        for part in (content.parts or [])
    )
    assert "[Context:" not in history_text


def test_local_system_prompt_ignores_mode_and_screen_arguments() -> None:
    cfg = load_config()
    cfg.backend = "local"

    first = build_system_prompt(cfg, "auto-safe", screen_line="Screen: browser=firefox")
    second = build_system_prompt(cfg, "ask", screen_line="Screen: browser=brave", skills_hint="Skills: x")

    assert first == second
    assert "set_timer" in first
    assert "take_screenshot" in first
    assert "type_text" in first
    assert "browser_tab_control" in first


def test_local_payload_pins_slot_zero_and_caches_the_static_prompt() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "Hello there."}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    cfg = load_config()
    cfg.backend = "local"
    adapter = LocalAdapter(cfg, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    contents = [genai_types.Content(role="user", parts=[genai_types.Part.from_text(text="hi")])]

    adapter.generate_turn(contents, "STATIC PROMPT")

    body = bodies[0]
    assert body["id_slot"] == 0
    assert body["cache_prompt"] is True
    assert body["max_tokens"] == 220
    assert body["messages"][0] == {"role": "system", "content": "STATIC PROMPT"}


def test_agent_loop_uses_220_tokens_on_every_local_call(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    adapter = CaptureAdapter(reply="Fine.")
    _loop(cfg, RecorderRegistry(), adapter).run_task("search for something")

    assert adapter.seen
    assert all(entry["max_tokens"] == 220 for entry in adapter.seen)


@pytest.mark.parametrize(
    ("cut", "expected"),
    [
        ("The main goals are on.", "The main goals are."),
        ("I can use it for.", "I can use it."),
        ("First complete sentence. It depends on.", "First complete sentence."),
        ("Search for information about AI and.", "Search for information about AI."),
        ("Choose the tools with.", "Choose the tools."),
        ("This is already complete.", "This is already complete."),
        ("What is this for?", "What is this for?"),
    ],
)
def test_clean_llm_reply_finishes_a_token_limited_sentence(cut: str, expected: str) -> None:
    assert clean_llm_reply(cut) == expected


def test_fast_path_turn_is_recorded_in_the_shared_history(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    adapter = CaptureAdapter(reply="It is twenty degrees.")
    loop = _loop(cfg, RecorderRegistry(), adapter)

    loop.record_external_turn(
        "open text editor",
        "open_app",
        {"app_name": "text editor"},
        "I've opened Text Editor for you.",
    )

    assert SESSION.last_reply == "I've opened Text Editor for you."
    assert len(loop.history) == 2
    history_text = "\n".join(
        getattr(part, "text", None) or ""
        for content in loop.history
        for part in (content.parts or [])
    )
    assert "open text editor" in history_text

    loop.run_task("what is the weather")
    assert "open text editor" in adapter.seen[-1]["text"]
    assert "I've opened Text Editor for you." in adapter.seen[-1]["text"]


# --------------------------------------------------------------------------------------
# Part 3/5: natural writing, "write it", >200 chars, clipboard paste
# --------------------------------------------------------------------------------------


def test_write_it_pastes_the_previous_reply() -> None:
    previous = "An AI agent is a program that perceives its environment and acts."
    SESSION.last_reply = previous
    registry = RecorderRegistry()

    reply = try_fast_path(registry, "Yes, right it")

    assert registry.calls == [("type_text", {"text": previous})]
    assert reply == "I've written that into your document."


def test_write_it_homophones_are_repaired_before_matching() -> None:
    assert normalise_spoken_command("right it") == "write it"
    assert normalise_spoken_command("yes right it") == "yes write it"
    assert normalise_spoken_command("okay right it") == "write it"
    assert normalise_spoken_command("okay right hello world") == "write hello world"


def test_write_a_topic_in_this_document_is_generated_not_dictated() -> None:
    assert match_fast_command("write what is an AI agent inside this new document") is None
    assert match_fast_command("write a short note in this tab") is None
    assert match_fast_command("I want you to write an AI agent inside this new tab") is None


def test_type_text_pastes_more_than_200_chars_with_wl_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_text = "An AI agent observes its environment and chooses actions. " * 6
    assert len(long_text) > 200
    expected = " ".join(long_text.split())
    copied: list[str] = []
    keys: list[list[str]] = []
    monkeypatch.setattr(dc, "_copy_clipboard", lambda text: copied.append(text) or True)
    monkeypatch.setattr(dc, "_ydotool_runner", lambda argv: keys.append(argv) or (True, ""))

    result = dc.type_text(long_text)

    assert result["ok"] is True
    assert result["method"] == "clipboard_paste"
    assert copied == [expected]
    assert keys == [["key", "29:1", "47:1", "47:0", "29:0"]]
    assert "longer than 200" not in json.dumps(result).lower()
    assert SESSION.last_typed_text == expected


def test_copy_clipboard_uses_wl_copy_with_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    monkeypatch.setattr(dc.shutil, "which", lambda name: "/usr/bin/wl-copy")
    monkeypatch.setattr(
        dc.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv) or Done(),
    )

    assert dc._copy_clipboard("hello world") is True
    assert calls == [["wl-copy", "--", "hello world"]]


def test_unified_history_open_then_write_generates_and_pastes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    long_paragraph = (
        "An AI agent is a system that perceives its environment through inputs, "
        "reasons about what it observes, and takes actions that move it toward a goal. "
        "Unlike a single prompt, an agent can plan several steps and use tools. "
        "Ultron is a small local example: it listens, chooses a tool, and reports the result."
    )
    assert len(long_paragraph) > 200
    copied: list[str] = []
    monkeypatch.setattr(dc, "_copy_clipboard", lambda text: copied.append(text) or True)
    monkeypatch.setattr(dc, "_ydotool_runner", lambda _argv: (True, ""))

    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(dc.type_text)
    adapter = CaptureAdapter(tool_call=("type_text", {"text": long_paragraph}))
    loop = _loop(cfg, registry, adapter)
    # This test pins the deterministic code-built reply; the environment uses the LLM.
    monkeypatch.setitem(get_state().live_config, "brain_replies", False)

    # This is exactly what listen.py/CLI do after the fast path opens the editor.
    loop.record_external_turn(
        "open text editor",
        "open_app",
        {"app_name": "text editor"},
        "I've opened Text Editor for you.",
    )
    result = loop.run_task("write what is an AI agent inside this new document")

    assert result.ok is True
    assert result.reply == "I've written that into your document."
    assert SESSION.last_typed_text == long_paragraph
    assert copied and copied[-1] == long_paragraph
    assert "open text editor" in adapter.seen[-1]["text"]
    assert "[Context:" in adapter.seen[-1]["text"]
    assert "longer than 200" not in json.dumps(result.reply).lower()


def test_repeat_request_uses_the_last_reply() -> None:
    SESSION.last_reply = "The battery is 82 percent."
    registry = RecorderRegistry()

    assert try_fast_path(registry, "what did you say? can you repeat it?") == SESSION.last_reply
    assert registry.calls == []


# --------------------------------------------------------------------------------------
# Part 4: Google default, site search, in-tab navigation, first results
# --------------------------------------------------------------------------------------


def test_google_is_the_default_and_site_phrases_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_ENGINE", raising=False)

    assert web.search_url("local llm").startswith("https://www.google.com/search?q=")
    assert web.parse_search_request("search f1 in youtube") == ("f1", "youtube")
    assert web.parse_search_request("search on YouTube for F1") == ("F1", "youtube")
    assert web.parse_search_request("search what is server in github") == ("what is server", "github")
    assert web.parse_search_request("search youtube") == ("", "youtube")
    assert web.parse_search_request("search for github") is None
    assert web.parse_search_request("search github in firefox") is None
    assert web.site_search_url("youtube", "") == "https://www.youtube.com"


@pytest.mark.parametrize(
    ("sentence", "arguments"),
    [
        ("search f1 in youtube", {"query": "f1", "site": "youtube"}),
        ("search on YouTube for f1", {"query": "f1", "site": "youtube"}),
        ("search what is server in github", {"query": "what is server", "site": "github"}),
        ("search youtube", {"query": "", "site": "youtube"}),
    ],
)
def test_fast_path_routes_natural_site_searches(sentence: str, arguments: dict[str, Any]) -> None:
    registry = RecorderRegistry()

    try_fast_path(registry, sentence)

    assert registry.calls == [("web_search", arguments)]


def test_empty_site_query_opens_the_site_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})

    result = web.web_search("", site="youtube")

    assert opened == ["https://www.youtube.com"]
    assert result["ok"] is True
    assert result["reply"] == "Opening YouTube."


def test_close_google_chrome_is_an_app_not_the_google_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    SESSION.note_app("firefox", browser=True)
    monkeypatch.setattr(
        dc,
        "browser_tab_control",
        lambda action: pytest.fail("close Google Chrome must not press Ctrl+W"),
    )
    monkeypatch.setattr(
        apps,
        "find_app",
        lambda _name: apps.AppMatch(key="chrome", display="Chrome", process_names=("chrome",)),
    )
    monkeypatch.setattr(apps, "_process_names_for", lambda _match: ("chrome",))

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return _done(argv, 0, "4242\n")

    monkeypatch.setattr(apps, "_process_runner", runner)

    result = apps.close_app("google chrome")

    assert result["ok"] is True and result["closed"] is True
    assert result["app_name"] == "Chrome"


def test_open_youtube_while_youtube_is_open_uses_the_current_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled: list[tuple[str, str]] = []
    SESSION.active_site = "youtube"
    SESSION.last_site = "youtube"
    SESSION.last_browser = "firefox"
    monkeypatch.setattr(
        web,
        "_open_in_current_tab",
        lambda url, browser: handled.append((url, browser)) or {"ok": True, "method": "current_tab"},
    )
    monkeypatch.setattr(web, "open_url", lambda *_a, **_k: pytest.fail("must not open a new tab"))

    result = web.web_search("f1", site="youtube")

    assert handled == [("https://www.youtube.com/results?search_query=f1", "firefox")]
    assert result["in_current_tab"] is True
    assert result["url"].startswith("https://www.youtube.com/results")


def test_navigate_current_tab_sends_ctrl_l_ctrl_v_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    keys: list[list[str]] = []
    monkeypatch.setattr(dc, "_copy_clipboard", lambda _url: True)
    monkeypatch.setattr(dc, "_ydotool_runner", lambda argv: keys.append(argv) or (True, ""))
    monkeypatch.setattr(dc, "_run", lambda *_a, **_k: _done([]))

    result = dc.navigate_current_tab("https://www.youtube.com/results?search_query=f1", "firefox")

    assert result["ok"] is True
    assert keys == [
        ["key", "29:1", "38:1", "38:0", "29:0"],
        ["key", "29:1", "47:1", "47:0", "29:0"],
        ["key", "28:1", "28:0"],
    ]


def test_first_result_never_opens_a_blank_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})
    monkeypatch.setattr(web, "fetch_first_result", lambda _q: "")

    assert web.is_blank_redirect("https://www.google.com/search?q=x&btnI=1") is True
    assert web.is_blank_redirect("https://duckduckgo.com/?q=!ducky+x") is True
    assert web.is_blank_redirect("https://www.google.com/search?q=x") is False

    result = web.open_first_result("python tutorial")

    assert opened == ["https://www.google.com/search?q=python+tutorial"]
    assert result["method"] == "search"
    assert "btnI" not in opened[0] and "ducky" not in opened[0]


@pytest.mark.parametrize(
    ("query", "url"),
    [("lutris", "https://lutris.net"), ("f1", "https://www.formula1.com")],
)
def test_open_first_result_uses_expanded_canonical_brands(
    query: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda target, browser=None: opened.append(target) or {"ok": True})

    result = web.open_first_result(query)

    assert opened == [url]
    assert result["method"] == "canonical"


# --------------------------------------------------------------------------------------
# Part 6/EPIPE: close tab, Flatpak, DEVNULL launches, aliases, stop phrase
# --------------------------------------------------------------------------------------


def test_close_github_routes_to_ctrl_w_when_a_browser_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    SESSION.note_app("firefox", browser=True)
    actions: list[str] = []
    monkeypatch.setattr(
        dc,
        "browser_tab_control",
        lambda action: actions.append(action) or {"ok": True, "action": action},
    )
    monkeypatch.setattr(apps, "find_app", lambda _name: pytest.fail("must not look for a GitHub app"))

    result = apps.close_app("github")

    assert actions == ["close_tab"]
    assert result["tab_closed"] is True
    assert close_app_reply({"ok": True, "raw_summary": result}) == "I've closed the GitHub tab."


def test_close_this_github_tab_routes_to_ctrl_w(monkeypatch: pytest.MonkeyPatch) -> None:
    SESSION.note_app("firefox", browser=True)
    actions: list[str] = []
    monkeypatch.setattr(
        dc,
        "browser_tab_control",
        lambda action: actions.append(action) or {"ok": True, "action": action},
    )

    result = apps.close_app("this github tab")

    assert actions == ["close_tab"]
    assert result["app_name"] == "GitHub"


def test_close_discord_uses_flatpak_kill_when_flatpak_ps_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 2.0) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["flatpak", "ps"]:
            return _done(argv, 0, "com.discordapp.Discord\n")
        return _done(argv, 0)

    monkeypatch.setattr(apps, "_flatpak_runner", runner)

    result = apps.close_app("discord")

    assert calls == [
        ["flatpak", "ps", "--columns=application"],
        ["flatpak", "kill", "com.discordapp.Discord"],
    ]
    assert result["ok"] is True and result["closed"] is True
    assert result["flatpak"] == "com.discordapp.Discord"
    assert close_app_reply({"ok": True, "raw_summary": result}) == "I've closed Discord for you."


def test_open_app_never_launches_a_gui_with_a_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProc:
        pid = 4321

        def poll(self) -> None:
            return None

    match = apps.AppMatch(key="firefox", display="Firefox", exec_argv=("firefox",))
    monkeypatch.setattr(apps, "find_app", lambda _name: match)
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        apps.subprocess,
        "Popen",
        lambda argv, **kwargs: captured.append((argv, kwargs)) or FakeProc(),
    )

    result = apps.open_app("firefox")

    assert result["ok"] is True
    argv, kwargs = captured[0]
    assert argv == ["firefox"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert "subprocess.PIPE" not in repr(kwargs)


def test_web_open_url_never_uses_a_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    class FakeProc:
        pid = 99

    monkeypatch.setattr(web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        web.subprocess,
        "Popen",
        lambda _argv, **kwargs: captured.append(kwargs) or FakeProc(),
    )

    assert web.open_url("https://example.org")["ok"] is True
    kwargs = captured[0]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_app_aliases_cover_lutris_and_text_editors() -> None:
    for name in ("loot risk", "lootris", "lu tris"):
        assert apps.app_key(name) == "lutris"
    for name in ("text editor", "document editor", "gedit", "notepad"):
        assert apps.app_key(name) == "text editor"
    # Hermes is its own desktop app, never a Lutris alias.
    assert apps.app_key("hermes") == "hermes"
    assert match_fast_command("open loot risk")["arguments"] == {"app_name": "lutris"}  # type: ignore[index]
    assert match_fast_command("open hermes")["arguments"] == {"app_name": "hermes"}  # type: ignore[index]
    assert match_fast_command("open notepad")["arguments"] == {"app_name": "text editor"}  # type: ignore[index]


def test_press_key_is_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(dc, "_ydotool_runner", lambda argv: calls.append(argv) or (True, ""))

    assert match_fast_command("press the enter key")["arguments"] == {"key": "enter"}  # type: ignore[index]
    assert dc.press_key("enter")["ok"] is True
    assert calls == [["key", "28:1", "28:0"]]

    refused = dc.press_key("ctrl+alt+delete")
    assert refused["ok"] is False
    assert "I don't press" in refused["error"]


def test_is_stop_listening_stops_the_listener() -> None:
    assert normalise_stop_command("is stop listening") == "stop listening"
    assert is_stop_command("is stop listening") is True

    cfg = load_config()
    cfg.backend = "local"
    cfg.listen_early_check_ms = 850
    cfg.voice_silence_ms = 1350
    cfg.recorder_warmup_ms = 300
    cfg.voice_min_snr_db = 10
    cfg.listen_after_speech_ms = 300
    commands: list[str] = []
    listener = HandsFreeListener(
        cfg,
        _Source(_silence(32) + _tone(20) + _silence(200)),
        transcribe=lambda _pcm: Transcript(True, "Ultron is stop listening", raw="Ultron is stop listening"),
        speak=lambda _text: None,
        command_runner=lambda command: (commands.append(command) or ("Done.", False)),
        print_fn=lambda _line: None,
    )

    listener.run()

    assert listener.stop_requested is True
    assert commands == []


# --------------------------------------------------------------------------------------
# Part 6: system tools, timer, screenshots and repeat
# --------------------------------------------------------------------------------------


def test_screenshot_date_timer_and_repeat_fast_paths() -> None:
    registry = RecorderRegistry()

    assert match_fast_command("take screenshots")["tool"] == "take_screenshot"  # type: ignore[index]
    reply = try_fast_path(registry, "take screenshots")
    assert reply == "I've saved the screenshot to /tmp/shot.png."

    assert match_fast_command("tell me the current date")["arguments"] == {"kind": "date"}  # type: ignore[index]
    assert try_fast_path(registry, "tell me the current date") == "Today is Monday."

    timer = match_fast_command("set a timer for 5 seconds")
    assert timer is not None and timer["arguments"] == {"seconds": 5}
    assert try_fast_path(registry, "set a timer for 5 seconds") == "I've set a timer for 5 seconds."

    ninety = match_fast_command("set a timer for 1 minute 30 seconds")
    assert ninety is not None and ninety["arguments"] == {"seconds": 90}


def test_timer_parsing_and_finish_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dc.parse_duration_seconds("5 seconds") == 5
    assert dc.parse_duration_seconds("2 minutes") == 120
    assert dc.parse_duration_seconds("1 minute 30 seconds") == 90
    assert dc.parse_duration_seconds("one minute and thirty seconds") == 90
    assert dc.parse_duration_seconds("nonsense") is None
    assert dc.format_duration(5) == "5 seconds"
    assert dc.format_duration(90) == "1 minute 30 seconds"

    spoken: list[str] = []
    notified: list[list[str]] = []
    monkeypatch.setattr(dc.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(
        dc.subprocess,
        "run",
        lambda argv, **_kwargs: notified.append(argv) or _done(argv),
    )
    monkeypatch.setattr(dc, "_timer_speaker", spoken.append)

    dc._timer_finished(5, "")

    assert spoken == ["Your 5 seconds timer is up."]
    assert notified and notified[0][:2] == ["notify-send", "Ultron Timer"]


def test_set_timer_is_non_blocking_and_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[int, Any, tuple[Any, ...]]] = []

    class FakeTimer:
        daemon = False

        def __init__(self, seconds: int, func: Any, args: tuple[Any, ...] = ()) -> None:
            started.append((seconds, func, args))

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(dc.threading, "Timer", FakeTimer)

    result = dc.set_timer(5)

    assert result["ok"] is True
    assert result["reply"] == "I've set a timer for 5 seconds."
    assert started and started[0][0] == 5

    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(dc.set_timer)
    names = {item["function"]["name"] for item in registry.get_openai_tools(compact=True)}
    assert "set_timer" in names


# --------------------------------------------------------------------------------------
# Part 7: memory teaching, email typing and the ChatML export
# --------------------------------------------------------------------------------------


def test_user_memory_alias_and_email_teaching(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    save_user_fact(cfg, "my email", "nitin@example.com")
    save_user_alias(cfg, "blue fox", "open firefox")

    assert memory_value("email", cfg) == "nitin@example.com"
    assert apps.app_key("blue fox") == "firefox"

    registry = RecorderRegistry()
    try_fast_path(registry, "enter the email")
    assert registry.calls == [("type_text", {"text": "nitin@example.com"})]


def test_agent_loop_teaches_aliases_and_facts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    loop = _loop(cfg, RecorderRegistry(), CaptureAdapter())

    alias_reply = loop.run_task("remember that blue fox means open firefox")
    assert "blue fox" in alias_reply.reply.lower()
    assert apps.app_key("blue fox") == "firefox"

    fact_reply = loop.run_task("remember that my email is nitin@example.com")
    assert "email" in fact_reply.reply.lower()
    assert memory_value("email", cfg) == "nitin@example.com"


def test_export_dataset_writes_valid_chatml(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.training_dataset_file.write_text(
        json.dumps(
            {
                "raw_transcript": "Ultron search f1 in youtube",
                "repaired_transcript": "search f1 in youtube",
                "tools_called": ["web_search"],
                "reply": "Searching YouTube for f1 right now.",
                "latency_ms": 12.0,
            }
        )
        + "\nnot-json\n",
        encoding="utf-8",
    )
    save_user_skill(cfg, "work mode", ["open code", "open github"])
    output = tmp_path / "ultron_finetune_chatml.jsonl"

    exported = export_chatml_dataset(cfg, output)

    assert exported["written"] == 2
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    for row in lines:
        roles = [message["role"] for message in row["messages"]]
        assert roles == ["system", "user", "assistant"]
        assert all(message["content"] for message in row["messages"])


def test_main_export_dataset_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "training_dataset.jsonl").write_text(
        json.dumps(
            {
                "repaired_transcript": "open firefox",
                "tools_called": ["open_app"],
                "reply": "I've opened Firefox for you.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "out.jsonl"

    result = subprocess.run(
        [sys.executable, "main.py", "export-dataset", str(output)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "ULTRON_WORKSPACE_DIR": str(workspace),
            "ULTRON_LOGS_DIR": str(tmp_path / "logs"),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rows and all([m["role"] for m in row["messages"]] == ["system", "user", "assistant"] for row in rows)


# --------------------------------------------------------------------------------------
# Part 3 config
# --------------------------------------------------------------------------------------


def test_brain_replies_default_and_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ULTRON_BRAIN_REPLIES", raising=False)
    cfg = load_config(env_path=tmp_path / "missing.env", settings_path=tmp_path / "missing.yaml")
    assert cfg.brain_replies == "auto"

    monkeypatch.setenv("ULTRON_BRAIN_REPLIES", "llm")
    cfg = load_config(env_path=tmp_path / "missing.env", settings_path=tmp_path / "missing.yaml")
    assert cfg.brain_replies == "llm"


# --------------------------------------------------------------------------------------
# audio helpers for the listener test
# --------------------------------------------------------------------------------------


def _tone(frames: int, amp: int = 3000) -> bytes:
    return array.array(
        "h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]
    ).tobytes()


def _silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


class _Source:
    def __init__(self, pcm: bytes) -> None:
        self.queue = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]

    def start(self) -> None:
        return None

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        if timeout == 0:
            return b""
        return self.queue.pop(0) if self.queue else None

    def stop(self) -> None:
        return None
