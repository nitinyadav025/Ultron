"""Gate 2D stability regressions: self-echo, screen vision, static LLM lane, telemetry.

Every test is deterministic and hardware-free: no mic, speaker, browser, GUI or network
beyond a localhost environment server. Live latency is checked separately by the Gate 2D
verification script.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from assistant.brain.agent_loop import (
    LOCAL_STATIC_TOOL_NAMES,
    AgentLoop,
    extract_action_tags,
    looks_like_pure_chat,
)
from assistant.brain.context import SESSION
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.brain.prompts import build_chat_prompt
from assistant.brain.reply_style import clean_llm_reply
from assistant.config import PROJECT_ROOT, load_config
from assistant.environment.launcher import TOOL_FUNCTIONS
from assistant.environment.server import EnvironmentServer
from assistant.environment.state import EnvironmentState
from assistant.interfaces.listen import is_self_echo
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.speech.mouth import PiperMouth
from assistant.tools.registry import ToolRegistry
from assistant.vision.screen_watcher import UIElement
from assistant.tools import screen_actions

DIST = PROJECT_ROOT / "assistant" / "dashboard" / "dist"


# --------------------------------------------------------------------------------------
# Part 1 — self-echo and hardware mic lock
# --------------------------------------------------------------------------------------


def test_self_echo_short_firefox_reply() -> None:
    assert is_self_echo("You're in firefox, mitten.", ["You're in Firefox, Nitin."]) is True


def test_self_echo_long_terminal_path_reply() -> None:
    heard = (
        "You are in need of access you call until the slash download slash Ultron, dot "
        "then slash bin slash python, main dot py environment on VS Code."
    )
    spoken = (
        "You are in 'nitinyadav@Sasuke:~/Downloads/ULTRON — .venv/bin/python main.py "
        "environment' on Vscode."
    )
    assert is_self_echo(heard, [spoken]) is True


def test_self_echo_never_drops_a_real_short_command() -> None:
    assert is_self_echo("open firefox", ["I've opened Firefox for you."]) is False
    assert is_self_echo("open firefox", ["Opening Firefox for you right now."]) is False
    assert is_self_echo("what is on my screen", ["You have Firefox open."]) is False


def test_mouth_mutes_before_playback_and_unmutes_after() -> None:
    events: list[list[str] | str] = []
    mouth = PiperMouth(load_config(), wpctl_runner=lambda argv: events.append(list(argv)))
    mouth._state = lambda: None  # never touch the shared live state
    mouth.synthesize_wav = lambda text: b"RIFF"
    mouth.play_wav_bytes = lambda wav: (events.append("PLAY"), (True, "fake"))[1]

    ok, _info = mouth.speak("Opening Firefox for you right now.")

    assert ok is True
    mute_index = events.index(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"])
    play_index = events.index("PLAY")
    unmute_index = events.index(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "0"])
    assert mute_index < play_index < unmute_index
    assert events[-1][:2] == ["wpctl", "set-volume"]
    assert mouth.is_speaking is False
    assert mouth.last_speech_end_time > 0
    assert any("Firefox" in text for text in mouth.recent_spoken_texts)


# --------------------------------------------------------------------------------------
# Part 2 — clean screen vision
# --------------------------------------------------------------------------------------


def test_read_screen_never_emits_paths_or_duplicate_chrome(monkeypatch) -> None:
    launcher = (
        "nitinyadav@Sasuke:~/Downloads/ULTRON — .venv/bin/python main.py environment"
    )
    monkeypatch.setattr(
        SESSION, "open_windows", [launcher, "YouTube - Mozilla Firefox", "Visual Studio Code"]
    )
    monkeypatch.setattr(SESSION, "focused_window", launcher)
    monkeypatch.setattr(SESSION, "focused_app", "code")
    monkeypatch.setattr(SESSION, "active_site", "youtube")
    monkeypatch.setattr(SESSION, "last_browser", "firefox")
    monkeypatch.setattr(
        screen_actions,
        "_elements",
        lambda force=False: [
            UIElement.from_dict(
                {"ref": "@e1", "role": "button", "name": "Main Menu", "window": "YouTube", "clickable": True}
            ),
            UIElement.from_dict(
                {"ref": "@e2", "role": "button", "name": "Show more", "window": "YouTube", "clickable": True}
            ),
            UIElement.from_dict(
                {"ref": "@e3", "role": "button", "name": "Show more", "window": "YouTube", "clickable": True}
            ),
            UIElement.from_dict(
                {"ref": "@e4", "role": "button", "name": "Subscribe", "window": "YouTube", "clickable": True}
            ),
        ],
    )

    result = screen_actions.read_screen()
    reply = result["reply"]

    assert "Mozilla Firefox open to YouTube" in reply
    assert "Visual Studio Code" in reply
    assert "Show more" in reply
    assert "nitinyadav@Sasuke" not in reply
    assert ".venv/bin/python" not in reply
    assert "~/Downloads/ULTRON" not in reply
    assert "Main Menu, Main Menu" not in reply
    assert "Main Menu" not in reply


# --------------------------------------------------------------------------------------
# Part 2b — final stability pass: mic never left muted, longer replies, fast screen read
# --------------------------------------------------------------------------------------


def test_ensure_mic_unmuted_sends_set_mute_zero() -> None:
    from assistant.speech.mic_input import DEFAULT_SOURCE, ensure_mic_unmuted

    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert ensure_mic_unmuted(runner=run, which=lambda _n: "/usr/bin/wpctl") is True
    assert calls == [["wpctl", "set-mute", DEFAULT_SOURCE, "0"]]


def test_ensure_mic_unmuted_is_skipped_under_pytest_without_a_runner() -> None:
    from assistant.speech.mic_input import ensure_mic_unmuted

    assert ensure_mic_unmuted() is False


def test_environment_voice_start_unmutes_before_streaming(monkeypatch) -> None:
    from assistant.environment import voice_controller as vc

    seen: list[str] = []
    monkeypatch.setattr(vc, "ensure_mic_unmuted", lambda: seen.append("unmute"))

    class _Source:
        def start(self) -> None:
            pass

        def read(self, _size: int, timeout: float = 0.0) -> bytes:
            time.sleep(0.01)
            return b""

        def stop(self) -> None:
            pass

    def _source_factory() -> _Source:
        seen.append("source")
        return _Source()

    controller = vc.EnvironmentVoiceController(
        load_config(), state=EnvironmentState(config_path=Path("/tmp/ultron-unmute-test.json")),
        source_factory=_source_factory,
    )
    controller.start()
    controller.stop()

    assert seen[0] == "unmute"
    assert "source" in seen


def test_conversational_reply_cap_allows_multiple_sentences() -> None:
    from assistant.brain.agent_loop import LOCAL_CHAT_MAX_TOKENS

    assert LOCAL_CHAT_MAX_TOKENS == 110
    assert LOCAL_CHAT_MAX_TOKENS > 34


def test_read_screen_uses_the_fresh_cached_snapshot(monkeypatch) -> None:
    for kwargs in (
        {"open_windows": ["YouTube - Mozilla Firefox"]},
        {"focused_window": "YouTube - Mozilla Firefox"},
        {"active_site": "youtube"},
        {"last_browser": "firefox"},
        {"screen_elements": [
            UIElement.from_dict(
                {"ref": "@e1", "role": "button", "name": "Subscribe", "window": "YouTube"}
            ).as_dict()
        ]},
        {"screen_elements_at": time.monotonic()},
    ):
        for key, value in kwargs.items():
            monkeypatch.setattr(SESSION, key, value)

    def _must_not_snapshot(**_kwargs: Any) -> Any:
        raise AssertionError("read_screen must answer from the cached snapshot")

    monkeypatch.setattr(screen_actions, "_snapshot", _must_not_snapshot)

    started = time.monotonic()
    result = screen_actions.read_screen()
    elapsed_ms = (time.monotonic() - started) * 1000

    assert result["ok"] is True
    assert "YouTube" in result["reply"]
    assert elapsed_ms < 50


def test_read_screen_refreshes_when_the_cache_is_stale(monkeypatch) -> None:
    monkeypatch.setattr(
        SESSION,
        "screen_elements",
        [UIElement.from_dict({"ref": "@e1", "role": "button", "name": "Old"}).as_dict()],
    )
    monkeypatch.setattr(SESSION, "screen_elements_at", time.monotonic() - 12.0)
    monkeypatch.setattr(
        screen_actions,
        "_elements",
        lambda force=False: [UIElement.from_dict({"ref": "@e1", "role": "button", "name": "Fresh"})],
    )

    result = screen_actions.read_screen()

    assert result["ok"] is True
    assert "Fresh" in result["visible_ui"]


# --------------------------------------------------------------------------------------
# Part 3 — static, tools-free conversational lane and complete sentences
# --------------------------------------------------------------------------------------


def test_gta_question_takes_the_tools_free_lane() -> None:
    assert looks_like_pure_chat(
        "Hey Ultron, my friends and I were playing GTA V yesterday, what do you think "
        "about open world games?"
    )
    assert looks_like_pure_chat("I was playing GTA V with friends.") is True
    assert looks_like_pure_chat("open firefox") is False


def test_chat_prompt_is_static_and_under_110_words() -> None:
    first = build_chat_prompt(load_config())
    second = build_chat_prompt(load_config())
    assert first == second
    assert len(first.split()) <= 110
    assert "no tools are available" in first
    assert "[ACTION:" in first


def test_clean_llm_reply_never_chops_a_terminated_sentence() -> None:
    assert clean_llm_reply("I see you have a page open for.") == (
        "I see you have a page open for."
    )
    assert clean_llm_reply("That sounds like a great time.") == (
        "That sounds like a great time."
    )


def test_action_tag_parsing() -> None:
    text, actions = extract_action_tags(
        '[ACTION: open_app("firefox")] Opening Firefox for you now.'
    )
    assert text == "Opening Firefox for you now."
    assert actions == [("open_app", {"app_name": "firefox"})]


class _ChatAdapter:
    """Duck-typed local adapter that records the exact request it receives."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, reply: str = "That sounds like a great time.") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> tuple[Any, TokenUsageStats]:
        self.calls.append({"system": system_instruction, "tools": tools})
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=None)],
            text=self.reply,
            function_calls=[],
        )
        return response, TokenUsageStats()


def _loop(adapter: _ChatAdapter) -> AgentLoop:
    config = load_config()
    config.backend = "local"
    gate = PermissionGate(config=config, mode="auto-safe")
    registry = ToolRegistry(config=config, gate=gate)
    for fn in TOOL_FUNCTIONS:
        registry.register(fn)
    watchdog = WatchdogMonitor(
        config.max_steps, config.wall_clock_timeout_sec, config.idle_standdown_sec
    )
    return AgentLoop(config, adapter, registry, watchdog)  # type: ignore[arg-type]


def test_conversational_turns_send_no_tools_and_the_same_prompt() -> None:
    adapter = _ChatAdapter()
    loop = _loop(adapter)

    loop.run_task("What do you think about open world games?")
    loop.run_task("Tell me something else about them.")

    assert [call["tools"] for call in adapter.calls] == [None, None]
    assert adapter.calls[0]["system"] == adapter.calls[1]["system"]


def test_tool_lane_sends_the_same_six_static_schemas_every_turn() -> None:
    adapter = _ChatAdapter()
    loop = _loop(adapter)

    loop.run_task("search for open world games")
    loop.run_task("open firefox now")

    assert len(adapter.calls) == 2
    first = [item["function"]["name"] for item in adapter.calls[0]["tools"]]
    second = [item["function"]["name"] for item in adapter.calls[1]["tools"]]
    assert first == second
    assert set(first) == set(LOCAL_STATIC_TOOL_NAMES)
    assert len(first) <= 6


# --------------------------------------------------------------------------------------
# Part 4 — non-zero telemetry and no-store static assets
# --------------------------------------------------------------------------------------


def test_state_seeds_non_zero_gpu_and_cache(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    telemetry = state.snapshot()["telemetry"]

    assert telemetry["gpu_vram_used_mb"] > 0
    assert telemetry["cache_efficiency_pct"] > 90
    assert telemetry["vram_used_mb"] == telemetry["gpu_vram_used_mb"]
    assert telemetry["vram_total_mb"] == telemetry["gpu_vram_total_mb"]
    assert telemetry["brain_readiness_pct"] == telemetry["cache_efficiency_pct"]

    state.update_telemetry(llama_server_alive=True)
    assert state.snapshot()["telemetry"]["cache_efficiency_pct"] > 90


def test_api_state_and_static_assets_are_never_zero_or_cached(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    state.set_status("listening")
    server = EnvironmentServer(load_config(), state=state, port=0)
    url = server.start()
    try:
        with urllib.request.urlopen(url + "/api/state", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        telemetry = payload["telemetry"]
        assert telemetry["gpu_vram_used_mb"] > 0
        assert telemetry["cache_efficiency_pct"] > 90

        with urllib.request.urlopen(url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
            cache_control = response.headers.get("Cache-Control", "")
        assert "no-store" in cache_control and "no-cache" in cache_control
        assert 'id="ov-cache-pct">96.4' in html
        assert 'id="hud-vram">1,614' in html
    finally:
        server.stop()


def test_dashboard_fallbacks_use_canonical_and_alias_fields() -> None:
    app_js = (DIST / "app.js").read_text(encoding="utf-8")
    assert "cache_efficiency_pct" in app_js
    assert "brain_readiness_pct" in app_js
    assert "gpu_vram_used_mb" in app_js
    assert "vram_used_mb" in app_js
