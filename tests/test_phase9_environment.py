"""Gate 2A — standalone environment, voice, browser bridge and HTTP/SSE API."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from google.genai import types

from assistant.brain.agent_loop import AgentLoop
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.config import load_config
from assistant.environment.state import get_state
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.tools.registry import ToolRegistry, tool
from assistant.environment.browser_bridge import BrowserBridgeHub, get_browser_bridge
from assistant.environment.server import EnvironmentServer
from assistant.environment.state import EnvironmentState
from assistant.environment.voice_controller import EnvironmentVoiceController
from assistant.speech.mouth import PiperMouth


# --------------------------------------------------------------------------------------
# EnvironmentState
# --------------------------------------------------------------------------------------


def test_state_defaults_turns_latency_and_snapshot(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    assert state.status == "starting"
    assert state.voice_mode == "continuous"  # the environment listens without a wake word
    assert state.get_config()["assistant_name"] == "Ultron"
    assert state.get_config()["brain_replies"] is True
    assert state.get_config()["listen_followup_s"] == 8.0

    state.set_status("listening")
    state.set_voice_mode("continuous")
    state.set_audio_levels(120, 900)
    state.set_transcript("hello")
    state.set_thought("thinking")
    state.set_action("open_app", {"app_name": "firefox"}, "running")
    state.set_planned_steps(["open firefox", "search github"])
    state.add_turn("user", "hello", source="voice")
    state.add_turn("assistant", "hi", tools_called=["open_app"], latency_ms=12.5)
    state.add_latency(transcribe_ms=10, llm_ms=20, tool_ms=5, total_ms=35)

    snapshot = state.snapshot()
    assert snapshot["status"] == "listening"
    assert snapshot["voice_mode"] == "continuous"
    assert snapshot["audio_level_rms"] == 120
    assert snapshot["current_action"]["tool"] == "open_app"
    assert snapshot["next_planned_steps"] == ["open firefox", "search github"]
    assert [turn["role"] for turn in snapshot["conversation_feed"]] == ["user", "assistant"]
    assert snapshot["telemetry"]["p50_ms"] == 35.0
    assert snapshot["telemetry"]["latency_series"][0]["total_ms"] == 35.0


def test_state_ring_buffers_are_bounded(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    for index in range(60):
        state.add_turn("user", f"turn {index}")
        state.add_latency(total_ms=float(index))
    snapshot = state.snapshot()
    assert len(snapshot["conversation_feed"]) == 50
    assert snapshot["conversation_feed"][-1]["text"] == "turn 59"
    assert len(snapshot["telemetry"]["latency_series"]) == 30
    assert snapshot["telemetry"]["p50_ms"] == 44.5
    assert snapshot["telemetry"]["p95_ms"] == 57.55


def test_state_config_persists_and_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "env.json"
    state = EnvironmentState(config_path=path)
    updated = state.update_config(
        {
            "assistant_name": "Jarvis",
            "reply_style": "concise",
            "brain_replies": "false",
            "voice_silence_ms": 99999,
            "unknown_key": "ignored",
        }
    )
    assert updated["assistant_name"] == "Jarvis"
    assert updated["reply_style"] == "concise"
    assert updated["brain_replies"] is False
    assert updated["voice_silence_ms"] == 10000  # clamped
    assert "unknown_key" not in updated
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["assistant_name"] == "Jarvis"

    reloaded = EnvironmentState(config_path=path)
    assert reloaded.get_config()["assistant_name"] == "Jarvis"


def test_state_wait_for_change_wakes_on_update(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    before = state.revision
    observed: list[int] = []

    def updater() -> None:
        time.sleep(0.1)
        state.set_status("listening")

    thread = threading.Thread(target=updater, daemon=True)
    thread.start()
    revision = state.wait_for_change(before, timeout=2.0)
    observed.append(revision)
    thread.join(timeout=1.0)
    assert observed and observed[0] > before


# --------------------------------------------------------------------------------------
# Server fixtures
# --------------------------------------------------------------------------------------


class FakeVoice:
    mouth = None

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def start(self) -> bool:
        self.calls.append("start")
        return True

    def stop(self) -> None:
        self.calls.append("stop")

    def mute_mic(self, value: bool) -> None:
        self.calls.append(("mic", value))

    def mute_tts(self, value: bool) -> None:
        self.calls.append(("tts", value))

    def barge_in(self) -> bool:
        self.calls.append("barge_in")
        return True

    def start_ptt(self) -> None:
        self.calls.append("ptt_start")

    def stop_ptt(self) -> None:
        self.calls.append("ptt_stop")

    def set_mode(self, mode: str) -> bool:
        self.calls.append(("mode", mode))
        return True

    def apply_config(self) -> None:
        self.calls.append("apply_config")


class FakeApp:
    def __init__(self, key: str, name: str) -> None:
        self.key = key
        self.name = name
        self.desktop_id = f"{key}.desktop"
        self.desktop_path = f"/usr/share/applications/{key}.desktop"
        self.exec_argv = (key,)
        self.try_exec = ""
        self.source = "system"
        self.generic = ""


@pytest.fixture()
def server_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = EnvironmentState(config_path=tmp_path / "env.json")

    def brain(text: str, source: str = "api") -> dict[str, Any]:
        return {"reply": f"Echo: {text}", "tools_called": ["demo"], "llm_ms": 7.5}

    voice = FakeVoice()
    hub = BrowserBridgeHub()
    applied: list[dict[str, Any]] = []
    import assistant.environment.server as server_module

    monkeypatch.setattr(
        server_module,
        "scan_desktop_apps",
        lambda: [FakeApp("firefox", "Firefox"), FakeApp("hermes", "Hermes")],
    )
    monkeypatch.setattr(
        server_module,
        "open_app",
        lambda name: {"ok": True, "app_name": name, "reply": f"I've opened {name}."},
    )
    import assistant.tools.apps as apps_module

    monkeypatch.setattr(apps_module, "_catalog", lambda: {})
    server = EnvironmentServer(
        load_config(),
        state=state,
        port=0,
        brain=brain,
        voice=voice,
        bridge=hub,
        dashboard_dir=tmp_path / "missing-dashboard",
        on_config=applied.append,
    )
    url = server.start()
    yield server, url, state, voice, hub, applied
    server.stop()


def _request(url: str, path: str, method: str = "GET", payload: dict[str, Any] | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read()
        return response.status, json.loads(body) if body else {}


def _request_text(url: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(url + path, timeout=5) as response:
        return response.status, response.read().decode("utf-8", "replace")


def test_server_health_state_and_placeholder(server_env) -> None:
    _server, url, state, _voice, _hub, _applied = server_env
    status, health = _request(url, "/api/health")
    assert status == 200
    assert health["ok"] is True and health["version"] == "phase9-env"
    assert "llama_server" in health and "voice_status" in health

    status, snapshot = _request(url, "/api/state")
    assert status == 200
    assert snapshot["live_config"]["assistant_name"] == "Ultron"
    assert snapshot["telemetry"]["gpu_vram_total_mb"] == 4096

    status, html = _request_text(url, "/")
    assert status == 200
    assert "Ultron Environment API" in html


def test_server_command_streams_thought_turn_and_latency(server_env) -> None:
    _server, url, state, _voice, _hub, _applied = server_env
    status, payload = _request(
        url, "/api/command", "POST", {"text": "hello", "speak": False}
    )
    assert status == 200
    assert payload == {
        "ok": True,
        "reply": "Echo: hello",
        "tools_called": ["demo"],
        "latency_ms": payload["latency_ms"],
    }
    snapshot = state.snapshot()
    assert snapshot["conversation_feed"][-1]["text"] == "Echo: hello"
    assert snapshot["telemetry"]["latency_series"]


def test_server_sse_sends_a_state_event(server_env) -> None:
    _server, url, state, _voice, _hub, _applied = server_env
    request = urllib.request.Request(url + "/api/stream")
    with urllib.request.urlopen(request, timeout=5) as response:
        lines = []
        for _ in range(10):
            line = response.readline().decode("utf-8").strip()
            if line:
                lines.append(line)
            if any(item.startswith("data: ") for item in lines):
                break
    assert lines and lines[0] == "retry: 1000"
    event = next(item for item in lines if item.startswith("event: "))
    assert event == "event: state"
    data = next(item for item in lines if item.startswith("data: "))
    snapshot = json.loads(data.removeprefix("data: "))
    assert snapshot["live_config"]["assistant_name"] == "Ultron"


def test_server_voice_control_modes_and_barge_in(server_env) -> None:
    _server, url, state, voice, _hub, _applied = server_env
    status, payload = _request(
        url,
        "/api/voice/control",
        "POST",
        {"action": "barge_in", "mode": "continuous"},
    )
    assert status == 200
    assert payload["voice_mode"] == "continuous"
    assert payload["result"] is True
    assert ("mode", "continuous") in voice.calls and "barge_in" in voice.calls
    assert state.snapshot()["voice_mode"] == "continuous"

    status, payload = _request(
        url, "/api/voice/control", "POST", {"action": "ptt_start"}
    )
    assert status == 200 and "ptt_start" in voice.calls


def test_server_config_is_live_and_persisted(server_env) -> None:
    _server, url, state, voice, _hub, applied = server_env
    status, payload = _request(
        url,
        "/api/config",
        "POST",
        {"assistant_name": "Jarvis", "voice_silence_ms": 3000, "permission_mode": "ask"},
    )
    assert status == 200
    assert payload["config"]["assistant_name"] == "Jarvis"
    assert state.get_config()["voice_silence_ms"] == 3000
    assert applied and applied[-1]["assistant_name"] == "Jarvis"
    assert "apply_config" in voice.calls

    status, payload = _request(url, "/api/config")
    assert payload["config"]["assistant_name"] == "Jarvis"


def test_server_apps_and_app_action(server_env) -> None:
    _server, url, _state, _voice, _hub, _applied = server_env
    status, payload = _request(url, "/api/apps")
    assert status == 200
    names = {row["name"] for row in payload["apps"]}
    assert {"Firefox", "Hermes"} <= names
    assert all({"id", "name", "desktop_id", "category", "is_running"} <= set(row) for row in payload["apps"])

    status, result = _request(
        url, "/api/apps/action", "POST", {"action": "open", "app_name": "calculator"}
    )
    assert status == 200
    assert result["ok"] is True and result["app_name"] == "calculator"


def test_server_browser_sync_tabs_and_action(server_env) -> None:
    _server, url, _state, _voice, hub, _applied = server_env
    status, synced = _request(
        url,
        "/api/browser/sync",
        "POST",
        {
            "browser": "brave",
            "tabs": [
                {"tabId": 11, "windowId": 1, "title": "WhatsApp",
                 "url": "https://web.whatsapp.com/", "active": True},
                {"tabId": 12, "windowId": 1, "title": "Gemini",
                 "url": "https://gemini.google.com/app", "active": False},
            ],
            "dom": {"tabId": 11, "special": {"text": "Unread: 2"}, "elements": [{"ref": "@b1", "label": "Chats"}]},
        },
    )
    assert status == 200 and synced["tab_count"] == 2

    status, tabs = _request(url, "/api/tabs")
    assert status == 200 and tabs["count"] == 2
    assert {tab["title"] for tab in tabs["tabs"]} == {"WhatsApp", "Gemini"}

    status, read = _request(url, "/api/tabs/action", "POST", {"action": "read", "tab": "WhatsApp"})
    assert status == 200 and read["ok"] and "Unread: 2" in read["text"]

    status, clicked = _request(
        url, "/api/tabs/action", "POST", {"action": "click", "tab": "Gemini", "target": "New chat"}
    )
    assert status == 200 and clicked["ok"] and clicked["action_id"]

    actions = hub.poll("brave")
    assert any(action["type"] == "click_dom" for action in actions)


def test_server_skills_round_trip(server_env) -> None:
    _server, url, _state, _voice, _hub, _applied = server_env
    status, saved = _request(
        url, "/api/skills", "POST", {"type": "fact", "name": "email", "value": "nitin@example.com"}
    )
    assert status == 200 and saved["facts"]["email"] == "nitin@example.com"

    status, saved = _request(
        url,
        "/api/skills",
        "POST",
        {"type": "skill", "trigger": "work mode", "actions": ["open code", "open github"]},
    )
    assert status == 200
    assert any(skill["trigger"] == "work mode" for skill in saved["skills"])

    status, listed = _request(url, "/api/skills")
    assert status == 200 and listed["facts"]["email"] == "nitin@example.com"

    status, removed = _request(
        url, "/api/skills", "DELETE", {"type": "fact", "name": "email"}
    )
    assert status == 200 and removed["removed"] is True
    assert "email" not in removed["facts"]


# --------------------------------------------------------------------------------------
# Browser bridge tools
# --------------------------------------------------------------------------------------


def test_browser_bridge_tools_list_switch_click_and_read() -> None:
    from assistant.tools.browser_tabs import (
        interact_with_tab,
        list_browser_tabs,
        switch_to_tab,
    )

    hub = get_browser_bridge()
    hub.reset()
    hub.sync(
        {
            "browser": "brave",
            "tabs": [
                {"tabId": 21, "windowId": 1, "title": "WhatsApp",
                 "url": "https://web.whatsapp.com/", "active": True},
                {"tabId": 22, "windowId": 1, "title": "Gemini",
                 "url": "https://gemini.google.com/app", "active": False},
            ],
            "dom": {
                "tabId": 21,
                "elements": [{"ref": "@b1", "label": "Chats"}],
                "special": {"text": "Nitin: hello", "items": ["Nitin"], "latest": ["hello"]},
            },
        }
    )

    listed = list_browser_tabs()
    assert listed["ok"] and listed["count"] == 2 and listed["bridge_connected"] is True

    switched = switch_to_tab("WhatsApp")
    assert switched["ok"] and switched["tab"]["tabId"] == 21

    clicked = interact_with_tab("Gemini", "click", target="New chat")
    assert clicked["ok"] and clicked["action"] == "click"

    read = interact_with_tab("WhatsApp", "read")
    assert read["ok"] and "Nitin: hello" in read["text"]

    actions = hub.poll("brave")
    assert any(action["type"] == "switch_tab" for action in actions)
    assert any(action["type"] == "click_dom" for action in actions)
    hub.reset()


# --------------------------------------------------------------------------------------
# Mouth barge-in
# --------------------------------------------------------------------------------------


class _FakePlayer:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


def test_mouth_stop_speaking_terminates_the_active_player(tmp_path: Path) -> None:
    mouth = PiperMouth(load_config(), voice_factory=lambda _model, _config: object())
    player = _FakePlayer()
    mouth.adopt_player(player)  # type: ignore[arg-type]
    assert mouth.is_speaking is True
    assert mouth.stop_speaking() is True
    assert player.terminated is True
    assert mouth.is_speaking is False


# --------------------------------------------------------------------------------------
# Voice controller
# --------------------------------------------------------------------------------------


class _FakeEars:
    last_confidence = {"verdict": "ok"}

    def __init__(self, text: str = "check whatsapp") -> None:
        self.text = text
        self.calls = 0

    def transcribe_samples(self, _samples, **kwargs):
        self.calls += 1
        return True, self.text


class _FakeMouth:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.stopped = False

    def speak(self, text: str):
        self.said.append(text)
        return True, "pw-play"

    def stop_speaking(self) -> bool:
        self.stopped = True
        return True


class _FakeBrain:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, text: str, source: str = "voice"):
        self.calls.append((text, source))
        return {"reply": "Sure, Nitin.", "tools_called": ["interact_with_tab"], "llm_ms": 12.0}


def test_voice_controller_continuous_turn_and_barge_in(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    state.set_voice_mode("continuous")
    ears, mouth, brain = _FakeEars(), _FakeMouth(), _FakeBrain()
    controller = EnvironmentVoiceController(
        load_config(), state=state, ears=ears, mouth=mouth, brain=brain
    )

    controller._handle_pcm(b"\x00\x00" * 3200, source="voice")
    snapshot = state.snapshot()
    assert brain.calls == [("check whatsapp", "voice")]
    assert mouth.said == ["Sure, Nitin."]
    assert [turn["role"] for turn in snapshot["conversation_feed"]] == ["user", "assistant"]
    assert snapshot["telemetry"]["latency_series"]

    assert controller.barge_in() is True
    assert mouth.stopped is True


def test_voice_controller_ptt_uses_no_silence_timeout(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    ears, mouth, brain = _FakeEars("hello from ptt"), _FakeMouth(), _FakeBrain()
    controller = EnvironmentVoiceController(
        load_config(), state=state, ears=ears, mouth=mouth, brain=brain
    )

    controller.start_ptt()
    controller._consume(b"\x00\x00" * 320)
    assert brain.calls == []  # no transcription while the button is held
    controller.stop_ptt()
    controller._consume(b"\x00\x00" * 320)
    assert brain.calls and brain.calls[0][0] == "hello from ptt"
    assert state.snapshot()["voice_mode"] == "ptt"


def test_voice_controller_wake_word_and_followup(tmp_path: Path) -> None:
    state = EnvironmentState(config_path=tmp_path / "env.json")
    state.set_voice_mode("wake_word")
    brain = _FakeBrain()
    controller = EnvironmentVoiceController(
        load_config(), state=state, ears=_FakeEars(), mouth=_FakeMouth(), brain=brain
    )

    assert controller._resolve_command("random chatter", source="voice", transcribe_ms=1.0) is False
    assert controller._resolve_command("Ultron what is your name", source="voice", transcribe_ms=1.0) is True
    assert brain.calls[-1][0] == "what is your name"
    # The follow-up window allows the next sentence without repeating the wake word.
    assert controller._resolve_command("open firefox", source="voice", transcribe_ms=1.0) is True
    assert brain.calls[-1][0] == "open firefox"


# --------------------------------------------------------------------------------------
# True LLM replies vs deterministic fallback
# --------------------------------------------------------------------------------------


class _ToolThenTextAdapter:
    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, final_text: str | None = None, fail_second: bool = False) -> None:
        self.final_text = final_text
        self.fail_second = fail_second
        self.calls = 0

    def select_model(self, role: str = "main") -> str:
        return "p9-fake"

    def generate_turn(self, *_args: Any, **_kwargs: Any):
        self.calls += 1
        if self.calls == 1:
            content = types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="open_app", args={"app_name": "firefox"}, id="call-1"
                        )
                    )
                ],
            )
        else:
            if self.fail_second:
                raise RuntimeError("llama-server unreachable")
            content = types.Content(
                role="model",
                parts=[types.Part.from_text(text=self.final_text or "Done.")],
            )
        return (
            types.GenerateContentResponse(candidates=[types.Candidate(content=content)]),
            TokenUsageStats(),
        )


def _llm_loop(adapter: Any) -> AgentLoop:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)

    @tool(risk_level="safe", read_only=False, name="open_app")
    def fake_open(app_name: str) -> dict[str, Any]:
        """Open an app."""
        return {"ok": True, "app_name": app_name.title()}

    registry.register(fake_open)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    return AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]


def test_brain_replies_true_uses_the_llm_confirmation() -> None:
    state = get_state()
    previous = state.live_config.get("brain_replies", True)
    state.live_config["brain_replies"] = True
    try:
        adapter = _ToolThenTextAdapter("Firefox is up and ready for you.")
        result = _llm_loop(adapter).run_task("open firefox")
    finally:
        state.live_config["brain_replies"] = previous

    assert result.ok is True
    assert result.reply == "Firefox is up and ready for you."
    assert adapter.calls == 2  # tool turn + a genuine final model sentence


def test_brain_replies_true_falls_back_when_the_brain_is_unreachable() -> None:
    state = get_state()
    previous = state.live_config.get("brain_replies", True)
    state.live_config["brain_replies"] = True
    try:
        adapter = _ToolThenTextAdapter(fail_second=True)
        result = _llm_loop(adapter).run_task("open firefox")
    finally:
        state.live_config["brain_replies"] = previous

    assert result.ok is True
    assert result.reply  # the deterministic tool sentence, never a crash
    assert "Firefox" in result.reply
