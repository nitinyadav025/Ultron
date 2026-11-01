"""Phase 5 Part C: 25-command benchmark with mocked OS runners.

The benchmark never opens a window, types a key, takes a screenshot, or calls an LLM.
It measures the real routing, tool-argument and spoken-reply code; the two agent-path
cases (timed mute and open-ended chat) use the real AgentLoop with stub adapters.
Latency here is CPU/routing latency, NOT live microphone/LLM latency.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from google.genai import types

from assistant.brain.agent_loop import AgentLoop
from assistant.brain.context import SESSION
from assistant.brain.fast_path import match_fast_command, run_fast_command
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.config import LOGS_DIR, load_config
from assistant.safety.killswitch import WatchdogMonitor
from assistant.tools.volume import TIMED_MUTE_REPLY

KNOWN_APPS = {
    "firefox": "Firefox",
    "files": "Files",
    "calculator": "Calculator",
    "settings": "Settings",
    "code": "Code",
    "brave": "Brave",
    "chrome": "Chrome",
}


@dataclass
class BenchCase:
    text: str
    expected_tool: str | None
    expected_args: dict[str, Any] | None
    expected_reply: str
    mode: str = "fast"  # fast | timed_mute | chat
    seed: dict[str, str] = field(default_factory=dict)


CASES: tuple[BenchCase, ...] = (
    BenchCase("open firefox", "open_app", {"app_name": "firefox"}, "Firefox is open."),
    BenchCase("close firefox", "close_app", {"app_name": "firefox"}, "Closed Firefox."),
    BenchCase("open files", "open_app", {"app_name": "files"}, "Files is open."),
    BenchCase("switch to firefox", "switch_app", {"app_name": "firefox"}, "Switching to Firefox."),
    BenchCase(
        "close it",
        "close_app",
        {"app_name": "firefox"},
        "Closed Firefox.",
        seed={"last_app": "firefox"},
    ),
    BenchCase(
        "open firefox and search for github",
        "web_search",
        {"query": "github", "browser": "firefox"},
        "Searching for github in Firefox.",
    ),
    BenchCase(
        "search for python in firefox",
        "web_search",
        {"query": "python", "browser": "firefox"},
        "Searching for python in Firefox.",
    ),
    BenchCase(
        "open the first link for github",
        "open_first_result",
        {"query": "github"},
        "Opening the first result for github.",
    ),
    BenchCase(
        "turn volume to 30 percent",
        "set_volume",
        {"percent": 30},
        "Volume is now set to 30 percent.",
    ),
    BenchCase("mute", "mute_audio", {"muted": True}, "Audio is now muted."),
    BenchCase("unmute", "mute_audio", {"muted": False}, "Audio is now unmuted."),
    BenchCase("stay silent for 10 seconds", None, None, TIMED_MUTE_REPLY, mode="timed_mute"),
    BenchCase(
        "list files in my downloads folder",
        "list_files",
        {"directory": "downloads"},
        "There are 3 items in your downloads folder.",
    ),
    BenchCase(
        "list files in banana folder",
        "list_files",
        {"directory": "banana"},
        "I couldn't find that folder.",
    ),
    BenchCase(
        "scroll down",
        "scroll_page",
        {"direction": "down", "amount": 5},
        "Scrolled down.",
    ),
    BenchCase(
        "scroll to the top",
        "scroll_page",
        {"direction": "top", "amount": 5},
        "Scrolled to the top.",
    ),
    BenchCase(
        "open a new tab",
        "browser_tab_control",
        {"action": "new_tab"},
        "Opened a new tab.",
    ),
    BenchCase(
        "next tab",
        "browser_tab_control",
        {"action": "next_tab"},
        "Switched to the next tab.",
    ),
    BenchCase(
        "close tab",
        "browser_tab_control",
        {"action": "close_tab"},
        "Closed the tab.",
    ),
    BenchCase("type hello world", "type_text", {"text": "hello world"}, "Typed that."),
    BenchCase(
        "switch window",
        "window_control",
        {"action": "next_window"},
        "Switched windows.",
    ),
    BenchCase(
        "what is my battery",
        "system_info",
        {"kind": "battery"},
        "The battery is 100 percent and full.",
    ),
    BenchCase("what time is it", "system_info", {"kind": "time"}, "It is 12:00."),
    BenchCase("hello", None, None, "Hello. I am here."),
    BenchCase(
        "explain recursion in one sentence",
        None,
        None,
        "Recursion is a function that calls itself.",
        mode="chat",
    ),
)


def _fake_result(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool == "open_app":
        app = str(args.get("app_name") or "")
        display = KNOWN_APPS.get(app)
        if display is None:
            return {"ok": False, "unknown": True, "app_name": app}
        return {"ok": True, "app_name": display}
    if tool == "close_app":
        app = str(args.get("app_name") or "")
        return {"ok": True, "app_name": KNOWN_APPS.get(app, app.title()), "closed": True, "running": True}
    if tool == "switch_app":
        app = str(args.get("app_name") or "")
        return {
            "ok": True,
            "app_name": KNOWN_APPS.get(app, app.title()),
            "running": True,
            "focused": True,
            "opened": False,
        }
    if tool == "web_search":
        return {
            "ok": True,
            "query": args.get("query"),
            "browser": args.get("browser") or "firefox",
        }
    if tool == "open_first_result":
        query = str(args.get("query") or "")
        return {"ok": True, "query": query, "reply": f"Opening the first result for {query}."}
    if tool == "set_volume":
        return {"ok": True, "requested_percent": args.get("percent")}
    if tool == "mute_audio":
        return {"ok": True, "muted": bool(args.get("muted"))}
    if tool == "list_files":
        directory = str(args.get("directory") or "")
        if "banana" in directory:
            return {"ok": False, "error": "folder not found"}
        return {"ok": True, "directory": directory, "count": 3, "items": []}
    if tool in ("scroll_page", "browser_tab_control", "window_control"):
        return {"ok": True, **{key: value for key, value in args.items()}}
    if tool == "type_text":
        return {"ok": True, "reply": "Typed that.", "text_chars": len(str(args.get("text") or ""))}
    if tool == "system_info":
        kind = str(args.get("kind") or "")
        if kind == "battery":
            reply = "The battery is 100 percent and full."
        elif kind == "time":
            reply = "It is 12:00."
        else:
            reply = "The battery is 100 percent and full. 8.0 GB of 16.0 GB RAM is available."
        return {"ok": True, "kind": kind, "reply": reply}
    return {"ok": False, "error": f"unmocked tool {tool}"}


class BenchmarkRegistry:
    """A ToolRegistry-shaped object that never touches the real OS."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gate = SimpleNamespace(tainted_by_untrusted=False)
        self.needs_confirmation_seen = False
        self.on_execute: Callable[[str], None] | None = None

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_request: str = "",
        dry_run: bool = False,
        action_preview: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, dict(arguments)))
        return _fake_result(tool_name, arguments)

    def get_openai_tools(self) -> list[dict[str, Any]]:
        return []


class NeverAdapter:
    """Timed mute must return before the LLM is called."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def select_model(self, role: str = "main") -> str:
        return "benchmark-no-llm"

    def generate_turn(self, *_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        raise AssertionError("the LLM must not be called for this benchmark case")


class StubChatAdapter:
    """Returns one fixed sentence after measuring the real lean-chat code path."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.tools_seen: list[Any] = []

    def select_model(self, role: str = "main") -> str:
        return "benchmark-chat-stub"

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
        content = types.Content(role="model", parts=[types.Part.from_text(text=self.reply)])
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


def _agent_reply(case: BenchCase, registry: BenchmarkRegistry) -> str:
    config = load_config()
    config.backend = "local"
    watchdog = WatchdogMonitor(
        config.max_steps, config.wall_clock_timeout_sec, config.idle_standdown_sec
    )
    adapter: Any = NeverAdapter() if case.mode == "timed_mute" else StubChatAdapter(case.expected_reply)
    loop = AgentLoop(config, adapter, registry, watchdog)  # type: ignore[arg-type]
    return loop.run_task(case.text).reply


def _run_one(case: BenchCase) -> dict[str, Any]:
    SESSION.reset()
    for key, value in case.seed.items():
        setattr(SESSION, key, value)
    registry = BenchmarkRegistry()
    route_ms = execute_ms = reply_ms = 0.0
    started = time.perf_counter()

    if case.mode == "fast":
        route_start = time.perf_counter()
        command = match_fast_command(case.text)
        route_ms = (time.perf_counter() - route_start) * 1000
        if command is None:
            actual_reply = "<no fast path>"
            actual_tool = None
            actual_args: dict[str, Any] | None = None
        else:
            actual_tool = command.get("tool")
            actual_args = dict(command.get("arguments") or {}) if actual_tool else None
            execute_start = time.perf_counter()
            actual_reply = run_fast_command(registry, command)
            execute_ms = (time.perf_counter() - execute_start) * 1000
            reply_start = time.perf_counter()
            reply_ms = (time.perf_counter() - reply_start) * 1000
    else:
        route_start = time.perf_counter()
        actual_reply = _agent_reply(case, registry)
        route_ms = (time.perf_counter() - route_start) * 1000
        actual_tool = None
        actual_args = None

    total_ms = (time.perf_counter() - started) * 1000
    calls_ok = (
        registry.calls == [(case.expected_tool, case.expected_args)]
        if case.expected_tool is not None
        else registry.calls == []
    )
    passed = (
        actual_tool == case.expected_tool
        and actual_args == case.expected_args
        and actual_reply == case.expected_reply
        and calls_ok
    )
    return {
        "text": case.text,
        "mode": case.mode,
        "expected_tool": case.expected_tool,
        "actual_tool": actual_tool,
        "expected_args": case.expected_args,
        "actual_args": actual_args,
        "expected_reply": case.expected_reply,
        "actual_reply": actual_reply,
        "calls": registry.calls,
        "pass": passed,
        "route_ms": round(route_ms, 3),
        "execute_ms": round(execute_ms, 3),
        "reply_ms": round(reply_ms, 3),
        "total_ms": round(total_ms, 3),
    }


def run_benchmark(
    report_path: Path | None = None, print_fn: Callable[[str], None] = print
) -> dict[str, Any]:
    """Run the 25 mocked cases and write the JSON report."""
    rows = [_run_one(case) for case in CASES]
    passed = sum(1 for row in rows if row["pass"])
    total = len(rows)
    report = {
        "mode": "mocked",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "average_total_ms": round(sum(row["total_ms"] for row in rows) / max(1, total), 3),
        "average_route_ms": round(sum(row["route_ms"] for row in rows) / max(1, total), 3),
        "cases": rows,
    }
    target = Path(report_path) if report_path else LOGS_DIR / "benchmark_phase5c.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print_fn("Phase 5 Part C benchmark (mocked OS runners)")
    print_fn(f"{'#':>2} {'command':38} {'tool':20} {'ok':3} {'ms':>8}")
    for index, row in enumerate(rows, 1):
        mark = "PASS" if row["pass"] else "FAIL"
        print_fn(
            f"{index:2d} {row['text'][:38]:38} {str(row['actual_tool']):20} {mark:4} {row['total_ms']:8.3f}"
        )
    print_fn(
        f"Result: {passed}/{total} passed, {total - passed} failed; average {report['average_total_ms']:.3f} ms"
    )
    print_fn(f"Report: {target}")
    print_fn("Latency is mocked routing/CPU time; live microphone/LLM latency is UNVERIFIED.")
    return report


if __name__ == "__main__":
    raise SystemExit(0 if run_benchmark()["failed"] == 0 else 1)
