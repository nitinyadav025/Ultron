"""Phase 8: real-time eyes (AT-SPI/agent-desktop/OCR), hands, small.en ears and gio apps.

Every test is deterministic. No real microphone, speaker, browser, keyboard, screenshot,
AT-SPI bus, dconf write, Flatpak sandbox or network call is used: every OS runner and every
accessibility client is injected, and `snapshot_screen` refuses to spawn the real helper
under pytest unless a fake is supplied.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from google.genai import types as genai_types

import assistant.brain.agent_loop as agent_loop_module
import assistant.config as config_module
from assistant.brain.agent_loop import AgentLoop
from assistant.brain.context import SESSION, context_tag, ensure_accessibility
from assistant.brain.fast_path import match_fast_command
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.brain.prompts import build_system_prompt
from assistant.brain.skills import configure_runtime
from assistant.config import ACCESSIBILITY_ENV, AssistantConfig, load_config
from assistant.safety.killswitch import WatchdogMonitor
from assistant.speech.ears import (
    BEST_OF,
    BEAM_SIZE,
    CORE_HOTWORDS,
    VAD_PARAMETERS,
    WhisperEars,
    build_dynamic_hotwords,
)
from assistant.speech.wake import normalise_spoken_command
from assistant.tools import apps, desktop_control as dc, screen_actions, web
from assistant.tools.screen_actions import click_element, focus_and_type, read_screen
from assistant.vision import screen_watcher
from assistant.vision.screen_watcher import (
    ScreenSnapshot,
    ScreenWatcher,
    UIElement,
    format_visible_ui,
    find_element,
    parse_agent_desktop_snapshot,
    parse_atspi_snapshot,
    parse_tesseract_tsv,
    snapshot_screen,
)

FRAME = 320


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    SESSION.reset()
    dc.set_terminal_context(False)
    dc._skip_focus_once = False
    monkeypatch.setattr(dc, "_focus_delay_s", 0.0)
    monkeypatch.setattr(dc, "_clipboard_paste_delay_s", 0.0)
    monkeypatch.setattr(screen_watcher, "WATCH_INTERVAL_S", 0.3)
    yield
    SESSION.reset()
    dc.set_terminal_context(False)
    dc._skip_focus_once = False


def _done(
    argv: list[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def _cfg(tmp_path: Any) -> AssistantConfig:
    cfg = load_config()
    cfg.backend = "local"
    cfg.learned_skills_file = tmp_path / "config_skills.json"
    cfg.user_skills_file = tmp_path / "user_skills.json"
    cfg.user_memory_file = tmp_path / "user_memory.json"
    cfg.training_dataset_file = tmp_path / "training_dataset.jsonl"
    cfg.learned_skills_file.write_text('{"version": 1, "skills": []}\n', encoding="utf-8")
    configure_runtime(cfg)
    return cfg


def _element(
    ref: str,
    name: str,
    role: str = "push button",
    *,
    x: int = 100,
    y: int = 100,
    w: int = 100,
    h: int = 40,
    editable: bool = False,
    actions: tuple[str, ...] = ("click",),
) -> UIElement:
    return UIElement(
        ref=ref,
        name=name,
        role=role,
        app="firefox",
        window="YouTube - Mozilla Firefox",
        x=x,
        y=y,
        w=w,
        h=h,
        editable=editable,
        actions=actions,
    )


def _publish(*elements: UIElement) -> None:
    SESSION.screen_elements = [element.as_dict() for element in elements]
    SESSION.focused_window = "YouTube - Mozilla Firefox"
    SESSION.focused_app = "firefox"
    SESSION.open_windows = ["YouTube - Mozilla Firefox"]
    SESSION.active_site = "youtube"
    SESSION.last_site = "youtube"
    SESSION.last_browser = "firefox"


class FakeAtspiClient:
    def __init__(self, available: bool = True, reply: dict[str, Any] | None = None) -> None:
        self.available_value = available
        self.reply = reply or {"ok": True, "elements": []}
        self.requests: list[dict[str, Any]] = []

    def available(self) -> bool:
        return self.available_value

    def request(self, payload: dict[str, Any], timeout: float = 4.0) -> dict[str, Any]:
        self.requests.append(dict(payload))
        return dict(self.reply)


class RecorderRegistry:
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
        return {"ok": True, "raw_summary": {"ok": True}}

    def get_openai_tools(self, compact: bool = False) -> list[dict[str, Any]]:
        return []


class CaptureAdapter:
    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, reply: str = "Okay.") -> None:
        self.reply = reply
        self.seen: list[dict[str, Any]] = []

    def select_model(self, role: str = "main") -> str:
        return "phase8-stub"

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
        self.seen.append({"system": system_instruction, "text": text})
        content = genai_types.Content(
            role="model", parts=[genai_types.Part.from_text(text=self.reply)]
        )
        return (
            genai_types.GenerateContentResponse(candidates=[genai_types.Candidate(content=content)]),
            TokenUsageStats(),
        )


def _loop(cfg: AssistantConfig, registry: Any, adapter: Any) -> AgentLoop:
    watchdog = WatchdogMonitor(
        cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec
    )
    return AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Part 1: accessibility bus + Chromium renderer accessibility
# --------------------------------------------------------------------------------------


def test_accessibility_environment_reaches_child_apps() -> None:
    assert ACCESSIBILITY_ENV == {
        "ACCESSIBILITY_ENABLED": "1",
        "QT_ACCESSIBILITY": "1",
    }
    assert os.environ["ACCESSIBILITY_ENABLED"] == "1"
    assert os.environ["QT_ACCESSIBILITY"] == "1"


def test_ensure_accessibility_sets_toolkit_and_introspect_with_a_runner() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return _done(argv)

    result = ensure_accessibility(runner=runner, session_type="wayland")

    assert result == {"toolkit_accessibility": True, "introspect": True}
    assert ["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"] in calls
    assert ["gsettings", "set", "org.gnome.shell", "introspect", "true"] in calls


def test_chromium_browsers_launch_with_renderer_accessibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    brave = apps.AppMatch(
        key="brave",
        display="Brave",
        exec_argv=("brave-browser",),
    )
    command = apps._launch_command_for(brave)
    assert command is not None
    assert command[0] == ["brave-browser", apps.CHROMIUM_A11Y_FLAG]
    assert apps.CHROMIUM_A11Y_FLAG == "--force-renderer-accessibility"


def test_web_url_in_a_chromium_browser_includes_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        web, "resolve_app_launch_command", lambda _name: (["brave-browser", "--force-renderer-accessibility"], "brave")
    )
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda argv, **_k: calls.append(argv) or SimpleNamespace(pid=7)
    )

    assert web.open_url("https://example.org", "brave")["ok"] is True
    assert calls == [["brave-browser", "--force-renderer-accessibility", "https://example.org"]]


# --------------------------------------------------------------------------------------
# Part 2: eyes — parsing, OCR, watcher, context tag, static system prompt
# --------------------------------------------------------------------------------------


ATSPI_PAYLOAD = {
    "ok": True,
    "source": "atspi",
    "windows": [
        {"app": "firefox", "title": "YouTube - Mozilla Firefox", "focused": True, "visible": True},
        {"app": "gnome-shell", "title": "Main stage", "focused": False, "visible": True},
    ],
    "focused_window": "YouTube - Mozilla Firefox",
    "focused_app": "firefox",
    "elements": [
        {
            "ref": "@e1",
            "name": "Show more",
            "role": "push button",
            "app": "firefox",
            "window": "YouTube - Mozilla Firefox",
            "x": 100,
            "y": 200,
            "w": 120,
            "h": 40,
            "cx": 160,
            "cy": 220,
            "focused": False,
            "editable": False,
            "actions": ["click"],
        },
        {
            "ref": "@e2",
            "name": "Search",
            "role": "entry",
            "app": "firefox",
            "window": "YouTube - Mozilla Firefox",
            "x": 10,
            "y": 20,
            "w": 300,
            "h": 36,
            "cx": 160,
            "cy": 38,
            "focused": True,
            "editable": True,
            "actions": ["SetFocus"],
        },
    ],
}


def test_parse_atspi_snapshot_returns_elements_and_ignores_shell_windows() -> None:
    snapshot = parse_atspi_snapshot(ATSPI_PAYLOAD)

    assert [item.ref for item in snapshot.elements] == ["@e1", "@e2"]
    assert snapshot.windows == ["YouTube - Mozilla Firefox"]
    assert snapshot.focused_window == "YouTube - Mozilla Firefox"
    assert snapshot.elements[1].editable is True
    assert snapshot.elements[0].cx == 160 and snapshot.elements[0].cy == 220
    assert format_visible_ui(snapshot.elements) == (
        '@e1:button:"Show more", @e2:entry:"Search"'
    )


def test_parse_agent_desktop_snapshot_maps_roles_and_keeps_native_refs() -> None:
    payload = {
        "ok": True,
        "data": {
            "app": "firefox",
            "tree": {
                "role": "window",
                "name": "YouTube",
                "children": [
                    {
                        "ref_id": "@s1:e1",
                        "role": "button",
                        "name": "Show more",
                        "available_actions": ["Click"],
                        "children": [
                            {
                                "ref_id": "@s1:e2",
                                "role": "textfield",
                                "name": "Search",
                                "available_actions": ["SetFocus"],
                            }
                        ],
                    }
                ],
            },
            "window": {"title": "YouTube - Mozilla Firefox"},
        },
    }
    snapshot = parse_agent_desktop_snapshot(payload, app="firefox")

    assert [item.ref for item in snapshot.elements] == ["@e1", "@e2"]
    assert snapshot.elements[0].role == "push button"
    assert snapshot.elements[1].role == "entry"
    assert snapshot.elements[0].native_ref == "@s1:e1"
    assert snapshot.source == "agent-desktop"


def test_parse_tesseract_tsv_keeps_words_with_boxes() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t120\t240\t80\t30\t92.5\tShow\n"
        "5\t1\t1\t1\t1\t2\t210\t240\t90\t30\t88.0\tmore\n"
        "5\t1\t1\t1\t1\t3\t900\t900\t20\t10\t12.0\tnoise\n"
    )
    elements = parse_tesseract_tsv(tsv)

    assert [item.name for item in elements] == ["Show", "more"]
    assert elements[0].ref == "@e1"
    assert (elements[0].x, elements[0].y, elements[0].w, elements[0].h) == (120, 240, 80, 30)
    assert (elements[0].cx, elements[0].cy) == (160, 255)
    assert all(item.source == "ocr" for item in elements)


def test_snapshot_prefers_native_atspi_and_renumbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeAtspiClient(reply=ATSPI_PAYLOAD)
    monkeypatch.setattr(screen_watcher, "agent_desktop_available", lambda: False)

    snapshot = snapshot_screen(atspi_client=client, allow_ocr=False)

    assert snapshot.source == "atspi"
    assert [item.ref for item in snapshot.elements] == ["@e1", "@e2"]
    assert client.requests[0]["cmd"] == "snapshot"


def test_ocr_fallback_uses_injected_screenshot_and_tesseract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screenshots: list[str] = []
    monkeypatch.setattr(screen_watcher, "agent_desktop_available", lambda: False)

    def screenshot(path: Any) -> bool:
        screenshots.append(str(path))
        return True

    tsv = (
        "5\t1\t1\t1\t1\t1\t50\t60\t70\t20\t90\tSkip\n"
        "5\t1\t1\t1\t1\t2\t130\t60\t80\t20\t90\tAd\n"
    )
    snapshot = snapshot_screen(
        atspi_client=FakeAtspiClient(available=False),
        screenshot_runner=screenshot,
        ocr_runner=lambda _path: tsv,
    )

    assert snapshot.source == "ocr"
    assert screenshots
    assert [item.name for item in snapshot.elements] == ["Skip", "Ad"]
    assert snapshot.elements[0].cx == 85


def test_screen_watcher_refresh_updates_session_and_stops() -> None:
    snapshot = ScreenSnapshot(
        elements=[_element("@e1", "Show more"), _element("@e2", "Search", "entry", editable=True)],
        windows=["YouTube - Mozilla Firefox"],
        focused_window="YouTube - Mozilla Firefox",
        focused_app="firefox",
        source="atspi",
    )
    watcher = ScreenWatcher(snapshot_fn=lambda: snapshot)

    assert watcher.refresh() is snapshot
    assert SESSION.screen_elements[0]["name"] == "Show more"
    assert SESSION.focused_window == "YouTube - Mozilla Firefox"
    assert SESSION.active_site == "youtube"
    assert SESSION.screen_source == "atspi"

    watcher.stop()
    assert watcher._stop.is_set()
    assert screen_watcher.start_screen_watcher() is None  # pytest guard: no real thread


def test_context_tag_contains_visible_ui_and_system_prompt_stays_static(tmp_path: Any) -> None:
    cfg = _cfg(tmp_path)
    cfg.backend = "local"
    _publish(_element("@e1", "Show more"), _element("@e2", "Search", "entry", editable=True))

    context = {
        "windows": ["YouTube - Mozilla Firefox"],
        "browser": "firefox",
        "site": "youtube",
        "focused_window": "YouTube - Mozilla Firefox",
        "focused_app": "firefox",
        "app": "firefox",
        "last_action": "opened firefox",
    }
    tag = context_tag(context)
    assert "| Visible UI: @e1:button:\"Show more\", @e2:entry:\"Search\"" in tag

    first = build_system_prompt(cfg, "auto-safe", screen_line="Screen: browser=firefox")
    SESSION.screen_elements = [_element("@e9", "Completely different").as_dict()]
    second = build_system_prompt(cfg, "ask", screen_line="Screen: browser=brave")
    assert first == second
    assert "Visible UI" not in first and "Screen:" not in first


def test_agent_loop_injects_visible_ui_only_into_the_user_turn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    registry = RecorderRegistry()
    adapter = CaptureAdapter(reply="I am here.")
    loop = _loop(cfg, registry, adapter)
    _publish(_element("@e1", "Show more"))
    monkeypatch.setattr(
        agent_loop_module,
        "get_screen_context",
        lambda: {
            "windows": ["YouTube"],
            "browser": "firefox",
            "site": "youtube",
            "focused_window": "YouTube",
            "focused_app": "firefox",
            "app": "firefox",
            "last_action": "opened firefox",
        },
    )
    loop.run_task("click the show more button")

    assert "Visible UI" in adapter.seen[0]["text"]
    assert "Show more" in adapter.seen[0]["text"]
    assert "Visible UI" not in adapter.seen[0]["system"]
    history_text = "\n".join(
        getattr(part, "text", None) or ""
        for content in loop.history
        for part in (content.parts or [])
    )
    assert "[Context:" not in history_text


# --------------------------------------------------------------------------------------
# Part 3: hands — three-tier click, read screen, focus + type
# --------------------------------------------------------------------------------------


def test_click_element_tier1_uses_the_atspi_action(monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(_element("@e1", "Show more"))
    calls: list[dict[str, Any]] = []

    def fake_atspi(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(payload))
        return {"ok": True, "method": "atspi_action"}

    monkeypatch.setattr(screen_actions, "_atspi", fake_atspi)

    result = click_element("Show more")

    assert result["ok"] is True
    assert result["tier"] == 1
    assert calls == [{"cmd": "click", "ref": "@e1"}]


def test_click_element_tier2_uses_ydotool_coordinates_after_semantic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(_element("@e1", "Show more", x=200, y=300, w=100, h=40))
    monkeypatch.setattr(
        screen_actions, "_atspi", lambda _payload: {"ok": False, "error": "no action"}
    )
    ydotool_calls: list[list[str]] = []
    monkeypatch.setattr(
        dc, "_ydotool_runner", lambda argv: ydotool_calls.append(list(argv)) or (True, "")
    )

    result = click_element("@e1")

    assert result["ok"] is True
    assert result["tier"] == 2
    assert result["coordinates"] == [250, 320]
    assert ydotool_calls == [
        ["mousemove", "--absolute", "250", "320"],
        ["click", "0xC0"],
    ]


def test_click_element_uses_ocr_boxes_when_atspi_has_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ocr_snapshot = ScreenSnapshot(
        elements=[
            UIElement(
                ref="@e1",
                name="Show more",
                role="text",
                x=20,
                y=40,
                w=100,
                h=30,
                source="ocr",
            )
        ],
        source="ocr",
    )
    monkeypatch.setattr(screen_actions, "ocr_screen_elements", lambda: ocr_snapshot)
    monkeypatch.setattr(
        screen_actions, "_atspi", lambda _payload: pytest.fail("OCR must not use AT-SPI")
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dc, "_ydotool_runner", lambda argv: calls.append(list(argv)) or (True, "")
    )

    result = click_element("Show more")

    assert result["ok"] is True
    assert result["tier"] == 2 and result["method"] == "ydotool"
    assert calls == [
        ["mousemove", "--absolute", "70", "55"],
        ["click", "0xC0"],
    ]


def test_click_element_tier3_uses_browser_find(monkeypatch: pytest.MonkeyPatch) -> None:
    SESSION.last_browser = "firefox"
    SESSION.active_site = "youtube"
    monkeypatch.setattr(screen_actions, "_focus_browser", lambda: None)
    monkeypatch.setattr(dc, "_copy_clipboard", lambda _text: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dc, "_ydotool_runner", lambda argv: calls.append(list(argv)) or (True, "")
    )

    result = click_element("Show more")

    assert result["ok"] is True
    assert result["tier"] == 3 and result["method"] == "browser_find"
    keys = [call for call in calls if call[0] == "key"]
    # Ctrl+F, Ctrl+V, Escape, Enter.
    assert keys[0] == ["key", "29:1", "33:1", "33:0", "29:0"]
    assert keys[1] == ["key", "29:1", "47:1", "47:0", "29:0"]
    assert keys[2] == ["key", "1:1", "1:0"]
    assert keys[3] == ["key", "28:1", "28:0"]


def test_expand_show_more_and_click_phrases_route_to_click_element() -> None:
    for phrase in (
        "expand the show more section",
        "click on Show more",
        "press the Show more button",
        "show more",
        "select Show more",
    ):
        command = match_fast_command(phrase)
        assert command is not None, phrase
        assert command["tool"] == "click_element", phrase
        assert "show more" in str(command["arguments"]["target"]).lower(), phrase

    assert normalise_spoken_command("expand the show more") == "click show more"
    assert normalise_spoken_command("show moor") == "show more"


def test_read_screen_summarises_window_site_and_buttons() -> None:
    _publish(
        _element("@e1", "Show more"),
        _element("@e2", "Search", "entry", editable=True),
        _element("@e3", "Subscribe"),
    )

    result = read_screen()

    assert result["ok"] is True
    assert "YouTube - Mozilla Firefox" in result["reply"]
    assert "YouTube" in result["reply"]
    assert "Show more" in result["reply"]
    assert result["elements"] == 3
    assert "@e1:button" in result["visible_ui"]


def test_read_screen_answers_a_query() -> None:
    _publish(_element("@e1", "Show more"), _element("@e2", "Subscribe"))

    found = read_screen("Subscribe")
    missing = read_screen("Obscure Thing")

    assert found["found"]["name"] == "Subscribe"
    assert "can see Subscribe" in found["reply"]
    assert missing["found"] is None
    assert "don't see" in missing["reply"]


def test_read_screen_phrases_route_from_the_fast_path() -> None:
    for phrase in (
        "what is on my screen",
        "what's on my screen",
        "read the screen",
        "what can I click",
        "what windows are open",
    ):
        command = match_fast_command(phrase)
        assert command is not None, phrase
        assert command["tool"] == "read_screen", phrase


def test_focus_and_type_focuses_the_field_and_pastes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(_element("@e1", "Search", "entry", editable=True, actions=("SetFocus",)))
    monkeypatch.setattr(screen_actions, "_atspi", lambda _payload: {"ok": True})
    monkeypatch.setattr(dc, "_copy_clipboard", lambda _text: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dc, "_ydotool_runner", lambda argv: calls.append(list(argv)) or (True, "")
    )

    result = focus_and_type("search", "f1 highlights")

    assert result["ok"] is True
    assert result["target_field"] == "search"
    assert result["method"] == "clipboard_paste"
    assert SESSION.last_typed_text == "f1 highlights"
    assert calls == [["key", "29:1", "47:1", "47:0", "29:0"]]


def test_focus_and_type_refuses_secret_text() -> None:
    _publish(_element("@e1", "Password", "password text", editable=True))

    result = focus_and_type("password", "my password is hunter2")

    assert result["ok"] is False
    assert "won't type" in result["error"]


def test_focus_and_type_phrase_routes_from_the_fast_path() -> None:
    command = match_fast_command("type hello world in the search box")

    assert command is not None
    assert command["tool"] == "focus_and_type"
    assert command["arguments"] == {"target_field": "search", "text": "hello world"}


def test_find_element_prefers_a_ref_exact_label_then_fuzzy() -> None:
    elements = [_element("@e1", "Show more"), _element("@e2", "Subscribe")]

    assert find_element("@e2", elements).name == "Subscribe"
    assert find_element("subscribe", elements).name == "Subscribe"
    assert find_element("show moar", elements).name == "Show more"
    assert find_element("does not exist", elements) is None


# --------------------------------------------------------------------------------------
# Part 4: ears — small.en, fast CPU decode, screen-primed hotwords, phonetic repair
# --------------------------------------------------------------------------------------


def test_load_config_defaults_to_small_en(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    models = tmp_path / "models"
    small = models / "faster-whisper-small.en"
    small.mkdir(parents=True)
    (small / "model.bin").write_bytes(b"weights")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_module, "MODELS_DIR", models)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.delenv("WHISPER_MODEL_DIR", raising=False)

    cfg = load_config(env_path=tmp_path / "missing.env", settings_path=tmp_path / "missing.yaml")

    assert AssistantConfig().whisper_model == "small.en"
    assert cfg.whisper_model == "small.en"
    assert cfg.whisper_model_dir == small


def test_missing_small_en_falls_back_to_base_en(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    models = tmp_path / "models"
    base = models / "faster-whisper-base.en"
    base.mkdir(parents=True)
    (base / "model.bin").write_bytes(b"weights")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config_module, "MODELS_DIR", models)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.delenv("WHISPER_MODEL_DIR", raising=False)

    cfg = load_config(env_path=tmp_path / "missing.env", settings_path=tmp_path / "missing.yaml")

    assert cfg.whisper_model == "base.en"
    assert cfg.whisper_model_dir == base


def test_ears_uses_beam_two_vad_parameters_and_small_en(tmp_path: Any) -> None:
    cfg = _cfg(tmp_path)
    cfg.whisper_model = "small.en"
    cfg.whisper_model_dir = tmp_path / "whisper"
    cfg.whisper_model_dir.mkdir()
    (cfg.whisper_model_dir / "model.bin").write_bytes(b"weights")
    cfg.whisper_hotwords = ""
    cfg.whisper_dynamic_hotwords = True
    calls: list[dict[str, Any]] = []

    class FakeModel:
        def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, Any]:
            calls.append(dict(kwargs))
            return iter([SimpleNamespace(text="open firefox")]), SimpleNamespace(language="en")

    ears = WhisperEars(cfg, model_factory=lambda _d: FakeModel())
    ok, text = ears.transcribe_samples(np.zeros(1600, dtype="float32"))

    assert ok is True and text == "open firefox"
    assert BEAM_SIZE == 2 and BEST_OF == 2
    assert calls[0]["beam_size"] == 2
    assert calls[0]["best_of"] == 2
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["vad_filter"] is True
    assert calls[0]["vad_parameters"] == VAD_PARAMETERS
    assert calls[0]["vad_parameters"] == {
        "min_silence_duration_ms": 250,
        "speech_pad_ms": 200,
    }
    assert calls[0]["hotwords"].startswith("Ultron, Firefox, Brave, GitHub")


def test_dynamic_hotwords_include_open_apps_and_eight_visible_labels() -> None:
    SESSION.focused_app = "firefox"
    SESSION.open_windows = ["YouTube - Mozilla Firefox"]
    labels = [
        "Show more",
        "Mute",
        "Subscribe",
        "Skip Ad",
        "Play",
        "Save",
        "Share",
        "Comments",
        "Ninth Label",
        "Tenth Label",
    ]
    SESSION.screen_elements = [
        _element(f"@e{index}", name, "push button").as_dict()
        for index, name in enumerate(labels, start=1)
    ]

    hotwords = build_dynamic_hotwords()

    for core in CORE_HOTWORDS:
        assert core in hotwords
    assert "firefox" in hotwords.lower()
    assert "Show more" in hotwords and "Comments" in hotwords
    assert "Ninth Label" not in hotwords and "Tenth Label" not in hotwords
    assert len(hotwords) <= 240


def test_wake_phonetic_repairs_for_screen_commands() -> None:
    assert normalise_spoken_command("show moor") == "show more"
    assert normalise_spoken_command("expand the show more") == "click show more"
    assert normalise_spoken_command("open loot risk") == "open lutris"
    assert normalise_spoken_command("open loo tris") == "open lutris"
    assert normalise_spoken_command("open hermes") == "open lutris"
    assert normalise_spoken_command("take a screen shot") == "take screenshot"
    assert normalise_spoken_command("dis cord") == "discord"
    assert normalise_spoken_command("right") == "write it"


# --------------------------------------------------------------------------------------
# Part 5: universal gio launch and natural app aliases
# --------------------------------------------------------------------------------------


def test_open_app_uses_gio_launch_with_devnull_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[list[str], dict[str, Any]]] = []
    match = apps.AppMatch(
        key="text editor",
        display="Text Editor",
        desktop_id="org.gnome.TextEditor.desktop",
        desktop_path="/usr/share/applications/org.gnome.TextEditor.desktop",
    )
    monkeypatch.setattr(apps, "find_app", lambda _name: match)
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        apps.subprocess,
        "Popen",
        lambda argv, **kwargs: captured.append((argv, kwargs)) or SimpleNamespace(pid=123, poll=lambda: None),
    )

    result = apps.open_app("text editor")

    assert result["ok"] is True
    argv, kwargs = captured[0]
    assert argv == [
        "gio",
        "launch",
        "/usr/share/applications/org.gnome.TextEditor.desktop",
    ]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_launch_order_is_gio_then_gtk_then_flatpak_then_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = apps.AppMatch(
        key="text editor",
        display="Text Editor",
        desktop_id="org.gnome.TextEditor.desktop",
        desktop_path="/usr/share/applications/org.gnome.TextEditor.desktop",
        exec_argv=("gnome-text-editor",),
    )
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert apps._launch_command_for(match)[0] == [  # type: ignore[index]
        "gio",
        "launch",
        "/usr/share/applications/org.gnome.TextEditor.desktop",
    ]

    monkeypatch.setattr(apps.shutil, "which", lambda name: None if name == "gio" else f"/usr/bin/{name}")
    assert apps._launch_command_for(match)[0] == [  # type: ignore[index]
        "gtk-launch",
        "org.gnome.TextEditor.desktop",
    ]

    flatpak_match = apps.AppMatch(
        key="discord",
        display="Discord",
        desktop_id="com.discordapp.Discord.desktop",
        yaml_entry={"flatpak_id": "com.discordapp.Discord"},
    )
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "flatpak" else None)
    assert apps._launch_command_for(flatpak_match)[0] == [  # type: ignore[index]
        "flatpak",
        "run",
        "com.discordapp.Discord",
    ]

    exec_match = apps.AppMatch(key="my tool", display="My Tool", exec_argv=("my-tool",))
    monkeypatch.setattr(apps.shutil, "which", lambda name: "/usr/bin/my-tool" if name == "my-tool" else None)
    assert apps._launch_command_for(exec_match)[0] == ["my-tool"]  # type: ignore[index]


def test_fedora_natural_aliases_resolve() -> None:
    apps.reset_app_cache()
    expected = {
        "terminal": "terminal",
        "console": "terminal",
        "shell": "terminal",
        "command prompt": "terminal",
        "settings": "settings",
        "system settings": "settings",
        "wifi": "settings",
        "bluetooth": "settings",
        "files": "files",
        "file manager": "files",
        "folder": "files",
        "downloads": "files",
        "text editor": "text editor",
        "notepad": "text editor",
        "calculator": "calculator",
        "software": "software",
        "app store": "software",
        "store": "software",
        "system monitor": "system monitor",
        "task manager": "system monitor",
        "lutris": "lutris",
        "games": "lutris",
        "loot risk": "lutris",
        "tweaks": "tweaks",
        "gnome tweaks": "tweaks",
        "extensions": "extensions",
    }
    for spoken, key in expected.items():
        assert apps.app_key(spoken) == key, spoken
        match = apps.find_app(spoken)
        # Lutris/Tweaks/Extensions are optional on a Fedora install; their spoken mapping
        # is still required. Everything else must resolve to a real desktop entry here.
        if match is None and key in {"lutris", "tweaks", "extensions"}:
            continue
        assert match is not None, spoken


def test_switch_app_relaunches_the_desktop_entry_when_wmctrl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[list[str]] = []
    match = apps.AppMatch(
        key="text editor",
        display="Text Editor",
        desktop_id="org.gnome.TextEditor.desktop",
        desktop_path="/usr/share/applications/org.gnome.TextEditor.desktop",
        process_names=("gnome-text-editor",),
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] == "pgrep":
            return _done(argv, 0, "4242\n")
        if argv[0] == "wmctrl":
            return _done(argv, 1)
        return _done(argv, 0)

    monkeypatch.setattr(apps, "find_app", lambda _name: match)
    monkeypatch.setattr(apps, "_process_runner", runner)
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        apps.subprocess,
        "Popen",
        lambda argv, **_k: launched.append(argv) or SimpleNamespace(pid=9, poll=lambda: None),
    )

    result = apps.switch_app("text editor")

    assert result["ok"] is True
    assert result["focused"] is True and result["opened"] is False
    assert launched == [
        ["gio", "launch", "/usr/share/applications/org.gnome.TextEditor.desktop"]
    ]


def test_focus_target_window_uses_wmctrl_when_it_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    match = apps.AppMatch(key="text editor", display="Text Editor")

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return _done(argv, 0)

    monkeypatch.setattr(apps, "find_app", lambda _name: match)
    monkeypatch.setattr(apps, "_process_runner", runner)
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = apps.focus_target_window("text editor")

    assert result["ok"] is True and result["method"] == "wmctrl"
    assert calls == [["wmctrl", "-a", "Text Editor"]]


def test_process_names_include_the_linux_15_char_comm_alias() -> None:
    long = apps.AppMatch(
        key="calculator", display="Calculator", process_names=("gnome-calculator",)
    )
    short = apps.AppMatch(key="files", display="Files", process_names=("nautilus",))

    assert apps._process_names_for(long) == ("gnome-calculator", "gnome-calculato")
    assert apps._process_names_for(short) == ("nautilus",)


def test_close_app_finds_a_process_name_truncated_to_15_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    match = apps.AppMatch(
        key="calculator",
        display="Calculator",
        desktop_id="org.gnome.Calculator.desktop",
        process_names=("gnome-calculator",),
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[0] == "pgrep" and argv[-1] == "gnome-calculato":
            return _done(argv, 0, "4242\n")
        if argv[0] == "pgrep":
            return _done(argv, 1)
        return _done(argv, 0)

    monkeypatch.setattr(apps, "find_app", lambda _name: match)
    monkeypatch.setattr(apps, "_process_runner", runner)
    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = apps.close_app("calculator")

    assert result["ok"] is True and result["closed"] is True
    assert result["pids"] == [4242]
    assert ["pgrep", "-x", "gnome-calculator"] in calls
    assert ["pgrep", "-x", "gnome-calculato"] in calls


def test_default_flatpak_runner_captures_ps_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "net.lutris.Lutris\n", "")

    monkeypatch.setattr(apps.subprocess, "run", fake_run)

    result = apps._default_flatpak_runner(
        ["flatpak", "ps", "--columns=application"], timeout=0.5
    )

    assert result.stdout.strip() == "net.lutris.Lutris"
    assert captured["kwargs"]["stdout"] == subprocess.PIPE
    assert captured["kwargs"]["stderr"] == subprocess.PIPE
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
