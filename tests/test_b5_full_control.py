"""Phase 5 B5: pause handling, browser/context, app discovery and desktop control.

Everything is deterministic. No real key is pressed, no window opens, no ydotool daemon
is started, and no screenshot is taken. The only real system reads are the .desktop
catalog count and the battery sysfs used by the read-only system-info tests.
"""

from __future__ import annotations

import array
import subprocess
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from assistant.brain.context import SESSION
from assistant.brain.fast_path import match_fast_command, try_fast_path
from assistant.brain.tool_replies import code_reply_for_tool
from assistant.config import load_config
from assistant.interfaces.listen import HandsFreeListener, Transcript, UtteranceDetector
from assistant.speech.recorder import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    SyntheticSource,
    record_until_silence,
    split_frames,
)
from assistant.safety.permissions import PermissionGate
from assistant.tools import apps, desktop_control as dc, web
from assistant.tools.registry import ToolRegistry, tool

FRAME = FRAME_SAMPLES


def tone(frames: int, amp: int = 3000) -> bytes:
    return array.array(
        "h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]
    ).tobytes()


def silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def completed(argv: list[str], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class Source:
    def __init__(self, pcm: bytes) -> None:
        self.queue: deque[bytes] = deque(
            [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
        )
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        if timeout == 0:
            return b""
        return self.queue.popleft() if self.queue else None

    def stop(self) -> None:
        self.stopped = True


# --------------------------------------------------------------------------------------
# Part 1: two-speed endpointing
# --------------------------------------------------------------------------------------


def test_short_command_finishes_at_1050ms_and_long_at_1450ms() -> None:
    detector = UtteranceDetector(
        warmup_ms=300, silence_ms=1450, early_ms=1050, min_snr_db=10.0, max_seconds=30.0
    )
    pcm = silence(32) + tone(20) + silence(150)  # 640 ms lead-in, 400 ms voice
    events: list[tuple[str, int]] = []
    for frame in split_frames(pcm):
        event = detector.feed(frame)
        if event:
            events.append((event, detector.audio_ms))
        if event == "final":
            break

    assert events[0][0] == "early"
    assert 2050 <= events[0][1] <= 2150  # 640 ms lead + 400 ms voice + 1.05 s early window
    assert events[-1][0] == "final"
    assert events[-1][1] - events[0][1] >= 350  # final waits the extra silence


def test_one_second_pause_keeps_one_recording() -> None:
    # 400 ms voice, a 1.0 s pause (< 1.05 s early check), 400 ms more voice.
    pcm = silence(32) + tone(20) + silence(50) + tone(20) + silence(150)
    transcriber_calls: list[bytes] = []
    commands: list[str] = []
    config = load_config()
    config.backend = "local"
    config.listen_early_check_ms = 1050
    config.voice_silence_ms = 1450
    config.recorder_warmup_ms = 300
    config.voice_min_snr_db = 10
    config.listen_after_speech_ms = 300

    def transcribe(audio: bytes) -> Transcript:
        transcriber_calls.append(audio)
        return Transcript(True, "Ultron open firefox and search for github")

    listener = HandsFreeListener(
        config,
        Source(pcm),
        transcribe=transcribe,
        speak=lambda _text: None,
        command_runner=lambda command: (commands.append(command) or ("Done.", False)),
        print_fn=lambda _line: None,
    )

    listener.run()

    assert transcriber_calls  # the sentence reached Whisper
    assert commands == ["open firefox and search for github"]
    assert listener.commands_run == 1


def test_voice_recording_keeps_a_one_second_pause() -> None:
    pcm = silence(32) + tone(60) + silence(50) + tone(60) + silence(150)
    recording = record_until_silence(SyntheticSource(pcm), silence_ms=1350)

    assert recording.saw_speech is True
    assert recording.reason == "silence"
    # Both 1.2 s speech halves are present; the 1.0 s pause did not end the capture.
    assert len(recording.pcm) / 2 / 16000 >= 3.2


# --------------------------------------------------------------------------------------
# Part 2: browser and session context
# --------------------------------------------------------------------------------------


def _fake_browser(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    class FakeProc:
        pid = 99

    monkeypatch.setattr(web, "resolve_known_app", lambda name: (["firefox"], "firefox"))
    monkeypatch.setattr(web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(web.subprocess, "Popen", lambda argv, **_k: calls.append(argv) or FakeProc())
    return calls


def test_conversational_prefix_still_uses_one_combined_search() -> None:
    command = match_fast_command("Can you open Firefox and search for Sadicing?")

    assert command is not None
    assert command["tool"] == "web_search"
    assert command["arguments"] == {"query": "sadicing", "browser": "firefox"}


def test_open_firefox_and_search_uses_only_firefox(monkeypatch: pytest.MonkeyPatch) -> None:
    SESSION.reset()
    command = match_fast_command("open firefox and search for github")
    assert command is not None
    assert command["tool"] == "web_search"
    assert command["arguments"] == {"query": "github", "browser": "firefox"}

    calls = _fake_browser(monkeypatch)
    result = web.web_search("github", "firefox")

    assert result["ok"] is True
    assert result["browser"] == "firefox"
    assert calls == [["firefox", web.search_url("github")]]
    assert all("xdg-open" not in argv for argv in calls)


def test_after_open_firefox_a_search_uses_firefox_not_xdg_open(monkeypatch: pytest.MonkeyPatch) -> None:
    SESSION.reset()
    SESSION.note_app("firefox", browser=True)
    calls = _fake_browser(monkeypatch)

    result = web.web_search("github")

    assert web.choose_browser() == "firefox"
    assert result["browser"] == "firefox"
    assert calls == [["firefox", web.search_url("github")]]


def test_a_search_also_sets_the_last_app_for_close_it(monkeypatch: pytest.MonkeyPatch) -> None:
    SESSION.reset()
    _fake_browser(monkeypatch)

    web.web_search("github", "firefox")

    assert SESSION.last_app == "firefox"
    close = match_fast_command("close it")
    assert close is not None and close["arguments"] == {"app_name": "firefox"}


def test_context_pronouns_use_last_app_and_last_query() -> None:
    SESSION.reset()
    SESSION.note_app("firefox", browser=True)
    SESSION.note_search("github", "firefox")

    close = match_fast_command("close it")
    first = match_fast_command("open the first link")

    assert close is not None and close["tool"] == "close_app"
    assert close["arguments"] == {"app_name": "firefox"}
    assert first is not None and first["tool"] == "open_first_result"
    assert first["arguments"] == {"query": "github", "browser": "firefox"}


def test_context_is_empty_at_the_start() -> None:
    SESSION.reset()
    close = match_fast_command("close it")
    first = match_fast_command("open the first link")

    assert close is not None and close["kind"] == "chat"
    assert first is not None and first["kind"] == "chat"


# --------------------------------------------------------------------------------------
# Part 3: dynamic .desktop discovery
# --------------------------------------------------------------------------------------


def _write_desktop(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")


@pytest.fixture()
def fake_desktop_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_desktop(
        tmp_path,
        "test-editor.desktop",
        "[Desktop Entry]\nType=Application\nName=Test Editor\nGenericName=Text Editor\nExec=test-editor %U\n",
    )
    _write_desktop(
        tmp_path,
        "hidden-tool.desktop",
        "[Desktop Entry]\nType=Application\nName=Hidden Tool\nExec=hidden-tool\nNoDisplay=true\n",
    )
    _write_desktop(
        tmp_path,
        "browser.desktop",
        "[Desktop Entry]\nType=Application\nName=Firefox\nGenericName=Web Browser\nExec=firefox %U\n",
    )
    monkeypatch.setattr(apps, "DESKTOP_DIRS", (tmp_path,))
    apps.reset_app_cache()
    yield tmp_path
    apps.reset_app_cache()


def test_dynamic_desktop_app_is_found_and_garbage_is_rejected(fake_desktop_dir: Path) -> None:
    match = apps.find_app("test editor")

    assert match is not None
    assert match.display == "Test Editor"
    assert match.exec_argv == ("test-editor",)
    assert apps.find_app("fierfox") is not None  # phonetic
    assert apps.find_app("some fake app") is None
    assert apps.find_app("the first link for github") is None


def test_dynamic_app_can_be_opened_and_closed(fake_desktop_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []

    class FakeProc:
        pid = 123
        returncode = None

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            raise subprocess.TimeoutExpired("test", timeout)

    monkeypatch.setattr(
        apps.shutil,
        "which",
        lambda name: None if name == "gtk-launch" else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(apps.subprocess, "Popen", lambda argv, **_k: launched.append(argv) or FakeProc())
    monkeypatch.setattr(apps, "_process_runner", lambda argv: completed(argv, 0, "4242\n"))

    opened = apps.open_app("test editor")
    closed = apps.close_app("test editor")

    assert opened["ok"] is True and opened["app_name"] == "Test Editor"
    assert launched == [["test-editor"]]
    assert closed["ok"] is True and closed["closed"] is True
    assert close_reply_for("test editor") == "I've closed Test Editor for you."


def close_reply_for(name: str) -> str:
    from assistant.brain.tool_replies import close_app_reply

    return close_app_reply(
        {"ok": True, "raw_summary": {"ok": True, "closed": True, "app_name": name.title()}}
    )


def test_switch_app_focuses_a_running_window(fake_desktop_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "pgrep":
            return completed(argv, 0, "4242\n")
        return completed(argv, 0)

    monkeypatch.setattr(apps.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(apps, "_process_runner", runner)

    result = apps.switch_app("test editor")

    assert result["ok"] is True
    assert result["running"] is True and result["focused"] is True
    assert calls == [["pgrep", "-x", "test-editor"], ["wmctrl", "-a", "Test Editor"]]
    assert code_reply_for_tool("switch_app", {"ok": True, "raw_summary": result}) == "I've switched to Test Editor."


def test_real_desktop_catalog_is_large() -> None:
    apps.reset_app_cache()
    discovered = apps.scan_desktop_apps()
    if not discovered:
        pytest.skip("no .desktop applications on this machine")
    assert len(discovered) >= 100
    assert apps.find_app("firefox") is not None
    assert apps.find_app("some fake app") is None


# --------------------------------------------------------------------------------------
# Part 4: desktop control (all runners mocked)
# --------------------------------------------------------------------------------------


def _capture_ydotool(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True) -> list[list[str]]:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[bool, str]:
        calls.append(argv)
        return ok, "" if ok else "no socket"

    monkeypatch.setattr(dc, "_ydotool_runner", runner)
    return calls


def test_type_text_uses_clipboard_then_ctrl_v(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_ydotool(monkeypatch)
    copied: list[str] = []
    monkeypatch.setattr(dc, "_copy_clipboard", lambda text: copied.append(text) or True)

    result = dc.type_text("hello world")

    assert result["ok"] is True and result["method"] == "clipboard_paste"
    assert copied == ["hello world"]
    assert calls == [["key", "29:1", "47:1", "47:0", "29:0"]]
    assert result["clipboard"] is True


def test_type_text_refuses_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_ydotool(monkeypatch)

    result = dc.type_text("my password is hunter2")

    assert result["ok"] is False
    assert calls == []


def test_type_text_falls_back_to_ydotool_when_the_clipboard_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[bool, str]:
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(dc, "_ydotool_runner", runner)
    monkeypatch.setattr(dc, "_copy_clipboard", lambda _text: False)

    result = dc.type_text("hello")

    assert calls == [["type", "--key-delay", "8", "--", "hello"]]
    assert result["ok"] is True and result["method"] == "ydotool"
    assert result["reply"] == "I've typed that into the active window."


def test_scroll_tab_window_and_media_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_ydotool(monkeypatch)

    assert dc.scroll_page("down", 3)["ok"] is True
    assert dc.scroll_page("top")["ok"] is True
    assert dc.browser_tab_control("new_tab")["ok"] is True
    assert dc.window_control("next_window")["ok"] is True
    assert dc.media_control("next")["ok"] is True

    # B6: PageDown (109), not three Down arrows (108) that print ^[[B in a terminal.
    assert calls[0] == ["key", "109:1", "109:0", "109:1", "109:0", "109:1", "109:0"]
    assert calls[1] == ["key", "102:1", "102:0"]
    assert calls[2] == ["key", "29:1", "20:1", "20:0", "29:0"]
    assert calls[3] == ["key", "56:1", "15:1", "15:0", "56:0"]
    assert calls[4] == ["key", "163:1", "163:0"]


def test_brightness_uses_gnome_dbus(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return completed(argv, 0)

    monkeypatch.setattr(dc, "_current_brightness", lambda: (10000, 20000))
    monkeypatch.setattr(dc, "_run", runner)

    result = dc.set_brightness(percent=50)

    assert result["ok"] is True and result["percent"] == 50
    assert calls[0][:3] == ["gdbus", "call", "--session"]
    assert calls[0][-1] == "<int32 50>"
    assert code_reply_for_tool("set_brightness", {"ok": True, "raw_summary": result}) == (
        "I've set the brightness to 50 percent."
    )


def test_brightness_failure_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc, "_current_brightness", lambda: None)
    monkeypatch.setattr(dc, "_run", lambda argv, timeout=5.0: completed(argv, 1))
    monkeypatch.setattr(dc.shutil, "which", lambda _name: None)

    result = dc.set_brightness(percent=40)

    assert result["ok"] is False
    assert "couldn't change" in result["error"]


def test_system_info_reads_battery_and_time() -> None:
    battery = dc.system_info("battery")
    clock = dc.system_info("time")
    status = dc.system_info("status")

    assert battery["ok"] is True and "battery" in battery["reply"].lower()
    assert clock["ok"] is True and clock["reply"].startswith("It is ")
    assert status["ok"] is True and "RAM" in status["reply"]


def test_screenshot_saves_to_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(dc.Path, "home", classmethod(lambda _cls: tmp_path))

    def runner(argv: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        if argv[0] == "gnome-screenshot":
            Path(argv[-1]).write_bytes(b"png")
        return completed(argv, 0)

    monkeypatch.setattr(dc, "_run", runner)
    monkeypatch.setattr(dc.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = dc.take_screenshot()

    assert result["ok"] is True
    assert Path(result["path"]).is_file()
    assert "I've saved the screenshot to" in code_reply_for_tool("take_screenshot", {"ok": True, "raw_summary": result})


# --------------------------------------------------------------------------------------
# Part 5: instant conversation
# --------------------------------------------------------------------------------------


class _NoToolRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

        @tool(name="open_app", risk_level="safe", read_only=False)
        def fake_open(app_name: str) -> dict[str, Any]:
            """Open an app."""
            self.calls.append(("open_app", {"app_name": app_name}))
            return {"ok": True, "app_name": app_name}

        cfg = load_config()
        gate = PermissionGate(config=cfg, mode="auto-safe")
        self.registry = ToolRegistry(config=cfg, gate=gate)
        self.registry.register(fake_open)


@pytest.mark.parametrize(
    ("text", "reply"),
    [
        ("hello", "Hello. I am here."),
        ("what is your name?", "I am Ultron, your local voice assistant."),
        ("what's your name?", "I am Ultron, your local voice assistant."),
        ("can you tell me what is your name", "I am Ultron, your local voice assistant."),
        ("who are you", "I am Ultron, your local voice assistant."),
        ("how are you", "I am running locally and ready to help."),
        ("thanks", "You're welcome."),
    ],
)
def test_instant_conversation_never_calls_a_tool(text: str, reply: str) -> None:
    holder = _NoToolRegistry()

    answer = try_fast_path(holder.registry, text)

    assert answer == reply
    assert holder.calls == []


def test_type_text_fast_path_preserves_case() -> None:
    command = match_fast_command("type Hello World")
    assert command is not None
    assert command["tool"] == "type_text"
    assert command["arguments"] == {"text": "Hello World"}


def test_type_text_accepts_a_conversational_prefix() -> None:
    command = match_fast_command("Can you please type Hello World")
    assert command is not None
    assert command["arguments"] == {"text": "Hello World"}


def test_switch_tab_and_increase_brightness_are_matched() -> None:
    switch = match_fast_command("switch tab")
    bright = match_fast_command("increase brightness")

    assert switch is not None and switch["arguments"] == {"action": "next_tab"}
    assert bright is not None and bright["arguments"] == {"percent": -1, "delta": 10}
