"""Phase 6: AGC guard, lexical priming, screen-aware site search, history cap, skills."""

from __future__ import annotations

import array
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from assistant.brain.agent_loop import AgentLoop
from assistant.brain.context import SESSION, get_screen_context, parse_window_titles, screen_line
from assistant.brain.fast_path import match_fast_command, try_fast_path
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.brain.skills import (
    load_skills,
    log_experience,
    match_skill,
    parse_teaching_command,
    prompt_hint,
    split_actions,
)
from assistant.config import PROJECT_ROOT, load_config
from assistant.interfaces import voice as voice_module
from assistant.safety.killswitch import WatchdogMonitor
from assistant.speech import mic_input
from assistant.speech.wake import normalise_spoken_command, strip_wake_prefixes, wake_match
from assistant.tools import desktop_control as desktop
from assistant.tools import web

PERMISSION_TESTS = (
    "test_confirm_tier_tools_still_ask",
    "test_destructive_tools_mislabelled_safe_still_ask",
    "test_ask_mode_still_confirms_every_non_read_only_call",
    "test_paths_outside_allowed_roots_still_ask",
    "test_untrusted_content_still_forces_confirmation",
    "test_blocked_tier_is_unchanged_and_never_prompts",
    "test_human_only_still_refuses_writes_with_instructions",
    "test_always_allow_isolated_to_one_folder",
    "test_registry_lets_a_safe_read_run_and_stops_a_mislabelled_write",
)


@pytest.fixture(autouse=True)
def _clean_session() -> Any:
    SESSION.reset()
    yield
    SESSION.reset()


def _cfg(tmp_path: Path):
    cfg = load_config()
    cfg.backend = "local"
    cfg.learned_skills_file = tmp_path / "config_skills.json"
    cfg.user_skills_file = tmp_path / "user_skills.json"
    cfg.training_dataset_file = tmp_path / "training_dataset.jsonl"
    cfg.learned_skills_file.write_text('{"version": 1, "skills": []}\n', encoding="utf-8")
    return cfg


class FakeRegistry:
    """Registry-shaped fake: records calls, touches no OS, prompts nobody."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gate = SimpleNamespace(tainted_by_untrusted=False, mode="auto-safe")
        self.needs_confirmation_seen = False
        self.allow_prompts = True
        self.on_execute = None

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_request: str = "",
        dry_run: bool = False,
        action_preview: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == "open_app":
            app = str(arguments.get("app_name") or "")
            return {"ok": True, "raw_summary": {"ok": True, "app_name": app.title()}}
        if tool_name == "web_search":
            site = str(arguments.get("site") or "")
            query = str(arguments.get("query") or "")
            url = (
                web.site_search_url(site, query)
                if site
                else web.search_url(query)
            )
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "query": query,
                    "site": site,
                    "browser": arguments.get("browser") or "firefox",
                    "url": url,
                },
            }
        if tool_name == "open_first_result":
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "query": arguments.get("query"),
                    "reply": "I've opened the first result for "
                    + str(arguments.get("query") or "")
                    + ".",
                },
            }
        if tool_name == "type_text":
            return {"ok": True, "raw_summary": {"ok": True, "reply": "I've typed that."}}
        return {"ok": True, "raw_summary": {"ok": True}}

    def get_openai_tools(
        self, compact: bool = False, names: Any | None = None
    ) -> list[dict[str, Any]]:
        return []


class TextAdapter:
    """Returns one plain sentence; records what the loop sent."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, reply: str = "Okay.") -> None:
        self.reply = reply
        self.seen: list[dict[str, Any]] = []

    def select_model(self, role: str = "main") -> str:
        return "phase6-stub"

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
        messages = list(contents)  # type: ignore[arg-type]
        self.seen.append(
            {
                "messages": len(messages),
                "tool_parts": sum(
                    1
                    for content in messages
                    for part in (getattr(content, "parts", None) or [])
                    if getattr(part, "function_response", None) is not None
                    or getattr(part, "function_call", None) is not None
                ),
                "system_chars": len(system_instruction),
                "history_chars": sum(
                    len(getattr(part, "text", None) or "")
                    for content in messages
                    for part in (getattr(content, "parts", None) or [])
                ),
                "system": system_instruction,
                "max_tokens": max_output_tokens,
                "contents_text": "\n".join(
                    getattr(part, "text", None) or ""
                    for content in messages
                    for part in (getattr(content, "parts", None) or [])
                ),
            }
        )
        content = types.Content(role="model", parts=[types.Part.from_text(text=self.reply)])
        return (
            types.GenerateContentResponse(candidates=[types.Candidate(content=content)]),
            TokenUsageStats(),
        )


def _loop(cfg, registry, adapter=None) -> AgentLoop:
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    return AgentLoop(cfg, adapter or TextAdapter(), registry, watchdog)  # type: ignore[arg-type]


# 1 — permission tests -----------------------------------------------------------------


def test_permission_module_still_has_the_nine_live_tests() -> None:
    source = (PROJECT_ROOT / "tests" / "test_permission_modes.py").read_text(encoding="utf-8")
    for name in PERMISSION_TESTS:
        assert f"def {name}(" in source, name


def test_permission_modes_run_with_zero_errors() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_permission_modes.py", "-q", "-p", "no:cacheprovider"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "12 passed" in result.stdout
    assert "error" not in (result.stdout + result.stderr).lower()


# 2 — dynamic mic AGC guard ------------------------------------------------------------


def test_agc_signature_and_restore_command() -> None:
    assert mic_input.agc_guard_needed(32768, 14064) is True
    assert mic_input.agc_guard_needed(32700, 3499) is False
    assert mic_input.agc_guard_needed(12000, 9000) is False

    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd: list[str], **_kwargs: Any) -> Done:
        calls.append(cmd)
        return Done()

    ok = mic_input.guard_mic_agc(
        32768,
        14064,
        "0.21",
        runner=fake_run,
        which=lambda _name: "/usr/bin/wpctl",
        background=False,
        force=True,
    )
    assert ok is True
    assert calls == [["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", "0.21"]]


def test_voice_recording_triggers_the_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.mic_source_volume = "0.21"
    controller = voice_module.VoiceController(cfg, adapter=None)
    seen: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        voice_module,
        "guard_mic_agc",
        lambda peak, rms, value="": seen.append((peak, rms, value)),
    )
    pcm = b"\x00\x00" * 320 + array.array("h", [32767, -32768] * 8000).tobytes()

    ok, _path = controller._finish_recording(pcm, 16000, 1, has_speech=True)

    assert ok is True
    assert seen and seen[0][0] >= mic_input.AGC_PEAK_THRESHOLD
    assert seen[0][1] > mic_input.AGC_RMS_THRESHOLD
    assert seen[0][2] == "0.21"


def test_browser_launch_schedules_a_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    timers: list[tuple[float, Any]] = []
    # web.py imports this lazily from assistant.speech.mic_input, so patch that module.
    monkeypatch.setattr(
        mic_input,
        "schedule_mic_volume_restore",
        lambda delay_s=1.5, value=None: timers.append((delay_s, value)),
    )

    class FakeProc:
        pid = 1

    monkeypatch.setattr(web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(web.subprocess, "Popen", lambda *_a, **_k: FakeProc())
    assert web.open_url("https://example.org")["ok"] is True
    assert timers and timers[0][0] == 1.5


def test_scheduler_is_deterministic_with_a_runner() -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    def fake_run(cmd: list[str], **_kwargs: Any) -> Done:
        calls.append(cmd)
        return Done()

    mic_input.restore_mic_volume(
        "", runner=fake_run, which=lambda _name: "/usr/bin/wpctl"
    )
    assert calls == [["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", "0.21"]]


# 3 — screen awareness and in-tab search ------------------------------------------------


def test_window_titles_update_the_session() -> None:
    output = (
        "0x01 0 host GitHub - Ultron - Mozilla Firefox\n"
        "0x02 0 host F1 - YouTube - Mozilla Firefox\n"
    )
    assert parse_window_titles(output) == [
        "GitHub - Ultron - Mozilla Firefox",
        "F1 - YouTube - Mozilla Firefox",
    ]

    class Done:
        returncode = 0
        stdout = output

    context = get_screen_context(runner=lambda *_a, **_k: Done())

    assert context["browser"] == "firefox"
    assert context["site"] == "youtube"
    assert SESSION.last_browser == "firefox"
    assert SESSION.active_site == "youtube"
    assert "site=youtube" in screen_line(context)


def test_search_f1_in_youtube_uses_the_youtube_url() -> None:
    registry = FakeRegistry()

    reply = try_fast_path(registry, "search f1 in youtube")

    assert registry.calls == [("web_search", {"query": "f1", "site": "youtube"})]
    assert reply == "Searching YouTube for f1 right now."


def test_search_f1_uses_youtube_already_on_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    handled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        web,
        "_open_in_current_tab",
        lambda url, browser: handled.append((url, browser)) or {"ok": True, "method": "current_tab"},
    )
    SESSION.active_site = "youtube"
    SESSION.last_browser = "firefox"

    result = web.web_search("f1")

    assert result["site"] == "youtube"
    assert result["in_current_tab"] is True
    assert handled == [("https://www.youtube.com/results?search_query=f1", "firefox")]


def test_navigate_current_tab_uses_ctrl_l_paste_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    keys: list[list[str]] = []
    monkeypatch.setattr(desktop, "_copy_clipboard", lambda _url: True)
    monkeypatch.setattr(desktop, "_ydotool_runner", lambda args: (keys.append(args), (True, ""))[1])
    monkeypatch.setattr(desktop, "_run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/wmctrl" if name == "wmctrl" else None)
    monkeypatch.setattr(desktop, "_focus_delay_s", 0.0)

    result = desktop.navigate_current_tab(
        "https://www.youtube.com/results?search_query=f1", "firefox"
    )

    assert result["ok"] is True
    assert len(keys) == 3
    assert all(call[0] == "key" for call in keys)


def test_first_result_never_uses_btni(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})
    result = web.open_first_result("github")
    assert opened == ["https://github.com"]
    assert result["method"] == "canonical"
    assert "btnI" not in opened[0]


# 4 — phonetic repair and wake aliases --------------------------------------------------


@pytest.mark.parametrize(
    ("heard", "command"),
    [
        ("Hey everyone open firefox", "open firefox"),
        ("Iron open firefox", "open firefox"),
        ("Here, drawn open firefox", "open firefox"),
        ("All drawn open firefox", "open firefox"),
        ("Altron open firefox", "open firefox"),
    ],
)
def test_new_wake_aliases_need_a_real_command(heard: str, command: str) -> None:
    match = wake_match(heard)
    assert match.found is True
    assert strip_wake_prefixes(heard) == command


def test_hey_ultron_still_matches() -> None:
    assert wake_match("Hey Ultron, open firefox").found is True


@pytest.mark.parametrize(
    ("heard", "repaired"),
    [
        ("Third YouTube", "search youtube"),
        ("Hurt YouTube", "search youtube"),
        ("Sir YouTube", "search youtube"),
        ("Third youtube f1", "search f1 in youtube"),
        ("right hello world", "write hello world"),
    ],
)
def test_phonetic_command_repair(heard: str, repaired: str) -> None:
    assert normalise_spoken_command(heard) == repaired


def test_iron_search_for_youtube_routes_to_web_search() -> None:
    match = wake_match("Iron Search for YouTube")
    assert match.found is True
    command = strip_wake_prefixes("Iron Search for YouTube")
    assert command == "search for youtube"

    registry = FakeRegistry()
    reply = try_fast_path(registry, command)
    assert registry.calls == [("web_search", {"query": "youtube", "browser": None})]
    assert "youtube" in reply.lower()


def test_third_youtube_routes_to_search() -> None:
    registry = FakeRegistry()
    reply = try_fast_path(registry, "Third YouTube")
    # Phase 7: a bare site name opens that site (an empty site query).
    assert registry.calls == [("web_search", {"query": "", "site": "youtube"})]
    assert "youtube" in reply.lower()


def test_play_first_video_uses_youtube() -> None:
    SESSION.note_search("f1", "firefox")
    command = match_fast_command("play first video")
    assert command is not None
    assert command["tool"] == "open_first_result"
    assert command["arguments"] == {"query": "f1", "site": "youtube", "first_video": True}


# 5 — history cap and flat prompt ------------------------------------------------------


def test_history_is_capped_and_old_tool_json_is_stripped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    adapter = TextAdapter()
    loop = _loop(cfg, FakeRegistry(), adapter)

    # Phase 9: a 16-turn / 32-message sliding window replaces the old 3-turn cap.
    for index in range(20):
        result = loop.run_task("hello")
        assert result.ok is True
        assert len(loop.history) <= 32, f"turn {index}: {len(loop.history)}"

    assert max(item["messages"] for item in adapter.seen) <= 32
    assert len({item["system_chars"] for item in adapter.seen}) == 1
    # After the cap is reached, the prompt stops growing (the last two turns are identical).
    assert adapter.seen[-1]["history_chars"] == adapter.seen[-2]["history_chars"]

    loop.history = [
        types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="open_app", args={"app_name": "firefox"}))],
        ),
        types.Content(
            role="user",
            parts=[types.Part.from_function_response(name="open_app", response={"ok": True})],
        ),
        types.Content(role="user", parts=[types.Part.from_text(text="hello")]),
        types.Content(role="model", parts=[types.Part.from_text(text="Hello.")]),
    ]
    loop._prune_history()
    assert all(
        getattr(part, "function_call", None) is None
        and getattr(part, "function_response", None) is None
        for content in loop.history
        for part in content.parts or []
    )


def test_screen_and_skills_hint_reach_the_turn_not_the_static_prompt(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    from assistant.brain.skills import save_user_skill

    save_user_skill(cfg, "work mode", ["do magic"])
    adapter = TextAdapter()
    loop = _loop(cfg, FakeRegistry(), adapter)
    loop.run_task("activate work mode")

    prompt = adapter.seen[-1]["system"]
    turn = adapter.seen[-1]["contents_text"]
    # Phase 7 moved all volatile text out of the system message so the KV cache survives.
    assert "Screen:" not in prompt
    assert "Skills:" not in prompt
    assert "[Context:" in turn
    assert "Skills:" in turn
    assert "work mode" in turn


# 6 — skills and experience dataset ----------------------------------------------------


def test_teaching_parses_and_persists(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    registry = FakeRegistry()
    loop = _loop(cfg, registry)

    learned = loop.run_task("remember when I say work mode, open code and open github")
    assert "learned" in learned.reply.lower()
    assert match_skill("work mode", cfg) is not None
    assert load_skills(cfg)[0].actions == ("open code", "open github")

    result = loop.run_task("work mode")
    assert result.ok is True
    assert registry.calls == [
        ("open_app", {"app_name": "code"}),
        ("open_app", {"app_name": "github"}),
    ]
    assert "opened" in result.reply.lower()


def test_teaching_forms_and_action_splitting(tmp_path: Path) -> None:
    assert parse_teaching_command("learn skill focus mode means open code; mute") == (
        "focus mode",
        ("open code", "mute"),
    )
    assert split_actions("open code and open github") == ("open code", "open github")
    assert prompt_hint("activate focus mode", _cfg(tmp_path)) == ""


def test_experience_log_is_private_and_complete(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    log_experience(
        cfg,
        raw_transcript="Iron Search for YouTube",
        repaired_transcript="search for youtube",
        tools_called=["web_search"],
        reply="Searching YouTube for youtube right now.",
        latency_ms=123.4,
        screen_context={"browser": "firefox", "site": "youtube"},
    )
    lines = cfg.training_dataset_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["raw_transcript"] == "Iron Search for YouTube"
    assert record["repaired_transcript"] == "search for youtube"
    assert record["tools_called"] == ["web_search"]
    assert record["screen_context"]["site"] == "youtube"
    assert record["latency_ms"] == 123.4
    assert "audio" not in record


# 7 — Whisper lexical priming on the saved clips ----------------------------------------


def _saved_wake_clip(name: str) -> Path:
    return PROJECT_ROOT / "workspace" / "wake_clips" / name


def test_lexical_prompt_improves_the_saved_single_ultron_clips(tmp_path: Path) -> None:
    model_dir = PROJECT_ROOT / "models" / "faster-whisper-base.en"
    clips = [
        _saved_wake_clip("wake_20261101-175906-374487_04.wav"),
        _saved_wake_clip("wake_20261101-175918-409966_05.wav"),
    ]
    if not model_dir.is_dir() or not all(clip.is_file() for clip in clips):
        pytest.skip("saved wake clips or the local Whisper model are not present")

    from assistant.speech.ears import WhisperEars, load_wav_16k_mono

    cfg = _cfg(tmp_path)
    cfg.whisper_model_dir = model_dir
    ears = WhisperEars(cfg)
    prompt = cfg.whisper_initial_prompt

    def transcribe(path: Path, prompt_on: bool) -> str:
        cfg.whisper_initial_prompt = prompt if prompt_on else ""
        _ok, text = ears.transcribe_samples(
            load_wav_16k_mono(path),
            log_text=False,
            hotwords=cfg.whisper_wake_hotwords,
        )
        return text

    off = [transcribe(clip, False) for clip in clips]
    on = [transcribe(clip, True) for clip in clips]
    assert sum(wake_match(text).found for text in on) >= sum(
        wake_match(text).found for text in off
    )
    assert all(wake_match(text).found for text in on)
    assert max(len(text) for text in on) < 120
