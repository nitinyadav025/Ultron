"""Phase 5 B6: deterministic replays of every Nov 2 live failure.

No microphone, speaker, browser, window, keyboard or LLM is touched. The only real model
use is Silero VAD on the saved real clips (skipped when a clip is missing), which proves
the bundled wrapper itself.
"""

from __future__ import annotations

import array
import json
import random
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from assistant.brain.agent_loop import AgentLoop
from assistant.brain.context import SESSION
from assistant.brain.fast_path import match_fast_command, run_fast_command, try_fast_path
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.brain.prompts import build_system_prompt
from assistant.brain.tool_replies import open_app_reply
from assistant.config import PROJECT_ROOT, load_config
from assistant.interfaces.listen import HandsFreeListener, Transcript, UtteranceDetector
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.speech.ears import guard_reason, looks_like_repetition_loop
from assistant.speech.recorder import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    SyntheticSource,
    record_until_silence,
    split_frames,
)
from assistant.speech.vad import SileroVad
from assistant.speech.wake import (
    is_safe_learned_alias,
    load_learned_aliases,
    strip_wake_prefixes,
    wake_match,
    wake_match_anywhere,
)
from assistant.tools import apps, desktop_control as dc, web
from assistant.tools.registry import ToolRegistry, tool

FRAME = FRAME_SAMPLES


def _gauss(ms: int, sigma: float, seed: int) -> bytes:
    rng = random.Random(seed)
    return array.array(
        "h", [max(-32768, min(32767, int(rng.gauss(0, sigma)))) for _ in range(ms * 16)]
    ).tobytes()


def _tone(frames: int, amp: int = 3000) -> bytes:
    return array.array(
        "h", [amp if i % 2 == 0 else -amp for _ in range(frames) for i in range(FRAME)]
    ).tobytes()


def _silence(frames: int) -> bytes:
    return b"\x00\x00" * FRAME * frames


def _utterance() -> bytes:
    """640 ms lead-in (past warm-up), 400 ms voice, 3 s of silence for the final check."""
    return _silence(32) + _tone(20) + _silence(150)


class FakeVad:
    """Speech when the tail contains a frame >= 2000 RMS (the 2400 speech, not the 1700 fan)."""

    def speech_ms_in_window(self, frames: Any, window_ms: int = 850, context_ms: int = 900) -> int:
        if not frames:
            return 0
        from assistant.speech.recorder import frame_rms

        return 300 if max(frame_rms(frame) for frame in frames) >= 2000 else 0


class NoSpeechVad:
    def speech_ms_in_window(self, _frames: Any, window_ms: int = 850, context_ms: int = 900) -> int:
        return 0


class AllSpeechVad:
    def speech_ms_in_window(self, _frames: Any, window_ms: int = 850, context_ms: int = 900) -> int:
        return 300


# --------------------------------------------------------------------------------------
# Part 1: Silero VAD + adaptive endpointing
# --------------------------------------------------------------------------------------


def test_vad_replay_stops_850ms_after_speech_despite_a_1700_fan() -> None:
    # The exact Nov 2 failure: floor 85, 2.0 s speech at RMS 2400, then a 10 s fan at 1700.
    pcm = _gauss(500, 100, 1) + _gauss(2000, 2400, 2) + _gauss(10000, 1700, 3)

    recording = record_until_silence(
        SyntheticSource(pcm), vad=FakeVad(), silence_ms=850, max_seconds=12.0
    )

    assert recording.saw_speech is True
    assert recording.reason == "silence"
    after_speech_ms = recording.duration_ms - 2500
    assert 850 <= after_speech_ms <= 1200
    assert recording.reason != "max_length"
    assert recording.trace["thresholds"]["vad"] is True


def test_detector_vad_replay_stops_within_1200ms_and_never_max_length() -> None:
    pcm = _gauss(500, 100, 1) + _gauss(2000, 2400, 2) + _gauss(10000, 1700, 3)
    detector = UtteranceDetector(
        warmup_ms=300, silence_ms=1350, early_ms=850, max_seconds=12.0, vad=FakeVad()
    )
    events: list[tuple[str, int]] = []
    for frame in split_frames(pcm):
        event = detector.feed(frame)
        if event:
            events.append((event, detector.audio_ms))
        if event == "final":
            break

    assert events and events[0][0] == "early"
    assert 3350 <= events[0][1] <= 3700  # 850-1200 ms after speech ends at 2500 ms
    assert detector.last_reason == "silence"
    assert detector.max_seconds == 12.0


def test_rolling_floor_tracks_a_louder_fan_instead_of_staying_at_85() -> None:
    # 1 s quiet room, then 3 s of fan at RMS 1700 with VAD hearing no speech.
    pcm = _gauss(1000, 85, 5) + _gauss(3000, 1700, 6)
    detector = UtteranceDetector(warmup_ms=300, max_seconds=12.0, vad=NoSpeechVad())

    for frame in split_frames(pcm):
        detector.feed(frame)

    assert detector.noise_floor > 1000
    assert detector.in_speech is False


def test_hard_cap_is_twelve_seconds_not_thirty() -> None:
    pcm = _gauss(500, 100, 7) + _gauss(20000, 3000, 8)
    recording = record_until_silence(
        SyntheticSource(pcm), vad=AllSpeechVad(), max_seconds=12.0
    )

    assert recording.reason == "max_length"
    assert 12.0 <= recording.duration_ms / 1000.0 <= 12.5


def test_real_silero_vad_is_bundled_and_detects_saved_speech() -> None:
    path = PROJECT_ROOT / "workspace" / "test_clips" / "attempt_20261101-134437-685173.wav"
    if not path.exists():
        pytest.skip("saved real clip is missing")

    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    audio = array.array("h")
    audio.frombytes(raw)
    speech_ms = SileroVad().speech_ms(audio)

    assert speech_ms is not None
    assert speech_ms >= 100


def test_real_silero_vad_says_the_fan_is_not_speech() -> None:
    path = PROJECT_ROOT / "workspace" / "test_clips" / "attempt_20261101-135201-414426.wav"
    if not path.exists():
        pytest.skip("the 30 s fan recording is missing")

    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    audio = array.array("h")
    audio.frombytes(raw)
    tail = audio[-16000:]  # the last second of the steady fan
    speech_ms = SileroVad().speech_ms_in_window(tail, window_ms=850, context_ms=900)

    assert speech_ms == 0


# --------------------------------------------------------------------------------------
# Part 2: wake aliases, error loop, prefix stripping
# --------------------------------------------------------------------------------------


def _listener(
    pcm: bytes,
    transcript: Transcript,
    *,
    learned: list[str] | None = None,
) -> tuple[HandsFreeListener, list[str], list[str], list[str]]:
    cfg = load_config()
    cfg.backend = "local"
    cfg.wake_word = "ultron"
    cfg.wake_aliases = ""
    cfg.listen_early_check_ms = 850
    cfg.listen_after_speech_ms = 300
    cfg.voice_silence_ms = 1350
    cfg.recorder_warmup_ms = 300
    cfg.voice_min_snr_db = 10
    commands: list[str] = []
    spoken: list[str] = []
    prints: list[str] = []
    listener = HandsFreeListener(
        cfg,
        _Source(pcm),
        transcribe=lambda _pcm: transcript,
        speak=spoken.append,
        command_runner=lambda command: (commands.append(command) or ("Done.", False)),
        print_fn=prints.append,
    )
    if learned is not None:
        listener.learned_aliases = learned
    return listener, commands, spoken, prints


class _Source:
    def __init__(self, pcm: bytes) -> None:
        self.queue = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def read(self, max_bytes: int, timeout: float) -> bytes | None:
        if timeout == 0:
            return b""
        return self.queue.pop(0) if self.queue else None

    def stop(self) -> None:
        self.stopped = True


def test_no_real_word_never_reaches_wake_match(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(*_args: Any, **_kwargs: Any) -> None:
        calls.append("wake_match")
        raise AssertionError("wake_match must not see an error string")

    monkeypatch.setattr("assistant.interfaces.listen.wake_match", boom)
    transcript = Transcript(
        False,
        "I didn't hear anything.",
        raw="I didn't hear anything.",
        reason="no_real_word",
    )
    listener, commands, spoken, prints = _listener(
        _utterance(), transcript, learned=["i didn", "i didn t", "what do"]
    )

    listener.run()

    assert calls == []
    assert commands == []
    assert spoken == []
    assert listener.ignored == 1
    assert listener.followup_until_ms == 0
    assert listener._followup_active is False
    assert any("heard noise, ignored" in line for line in prints)


def test_repeated_wake_words_are_not_a_repetition_loop() -> None:
    text = "Hey Ultron, Hey Ultron, Hey Ultron"

    assert looks_like_repetition_loop(text) is False
    assert guard_reason(text, []) == ""
    assert strip_wake_prefixes(text) == ""

    listener, commands, spoken, _prints = _listener(
        _utterance(), Transcript(True, text, raw=text)
    )
    listener.run()

    assert commands == []
    assert spoken == ["Yes?"]


def test_learned_alias_file_cannot_reintroduce_the_error_loop() -> None:
    assert is_safe_learned_alias("i didn") is False
    assert is_safe_learned_alias("i didn t") is False
    assert is_safe_learned_alias("i ll") is False
    assert is_safe_learned_alias("i m") is False
    assert is_safe_learned_alias("what do") is False
    assert is_safe_learned_alias("what do you") is False
    assert is_safe_learned_alias("hey all") is False
    assert is_safe_learned_alias("<rejected: I didn't catch that>") is False
    assert is_safe_learned_alias("outron") is True
    assert is_safe_learned_alias("i'll turn") is True


def test_wake_train_never_saves_a_rejected_decode_or_common_words(tmp_path: Path) -> None:
    from assistant.interfaces.wake_train import run_wake_train

    class RejectedEars:
        def transcribe_samples(self, _samples: Any, **_kwargs: Any) -> tuple[bool, str]:
            return False, "I didn't catch that"

    aliases = tmp_path / "wake_aliases.json"
    code = run_wake_train(
        load_config(),
        capture=lambda: _tone(20) + _silence(20),
        ears=RejectedEars(),
        small_ears=None,
        prompt=lambda _line: None,
        clips_dir=tmp_path / "wake_clips",
        aliases_path=aliases,
    )

    assert code == 0
    payload = json.loads(aliases.read_text(encoding="utf-8"))
    assert payload["aliases"] == {}


def test_ills_turn_is_stripped_and_routes_to_code() -> None:
    text = "I'll turn open VS code"
    match = wake_match(text)

    assert match.found is True
    assert match.target == "i'll turn"
    assert strip_wake_prefixes(text) == "open vs code"

    command = match_fast_command(text)
    assert command is not None
    assert command["tool"] == "open_app"
    assert command["arguments"] == {"app_name": "code"}

    listener, commands, _spoken, _prints = _listener(_utterance(), Transcript(True, text, raw=text))
    listener.run()
    assert commands == ["open vs code"]


def test_mid_sentence_wake_word_joins_both_commands() -> None:
    text = "Open Steam, Ultron can you open Discord for me?"
    match = wake_match_anywhere(text)

    assert match.found is True
    assert match.command == "open steam and can you open discord for me"

    command = match_fast_command(text)
    assert command is not None and command["kind"] == "compound"
    assert [item["tool"] for item in command["commands"]] == ["open_app", "open_app"]
    assert [item["arguments"] for item in command["commands"]] == [
        {"app_name": "steam"},
        {"app_name": "discord"},
    ]


# --------------------------------------------------------------------------------------
# Part 4/5: every exact phrase from the Nov 2 log
# --------------------------------------------------------------------------------------


class RecordingRegistry:
    """A registry-shaped fake; no OS call, no permission prompt, no LLM."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gate = SimpleNamespace(tainted_by_untrusted=False)
        self.needs_confirmation_seen = False
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
            display = app.title()
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "app_name": display,
                    "web_app": True,
                    "reply": f"I've opened {display} in Firefox.",
                },
            }
        if tool_name == "web_search":
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "query": arguments.get("query"),
                    "browser": arguments.get("browser") or "firefox",
                },
            }
        if tool_name == "open_first_result":
            query = str(arguments.get("query") or "")
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "query": query,
                    "reply": f"I've opened the first result for {query}.",
                },
            }
        if tool_name == "switch_app":
            app = str(arguments.get("app_name") or "")
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "app_name": app.title(),
                    "running": True,
                    "focused": True,
                    "opened": False,
                },
            }
        if tool_name == "browser_tab_control":
            return {"ok": True, "raw_summary": {"ok": True, "action": arguments.get("action")}}
        if tool_name == "scroll_page":
            return {
                "ok": True,
                "raw_summary": {
                    "ok": True,
                    "direction": arguments.get("direction"),
                    "amount": arguments.get("amount", 1),
                },
            }
        return {"ok": True, "raw_summary": {"ok": True}}


@pytest.fixture(autouse=True)
def _reset_session_and_focus() -> Any:
    SESSION.reset()
    dc.set_terminal_context(False)
    dc._skip_focus_once = False
    old_delay = dc._focus_delay_s
    dc._focus_delay_s = 0.0
    yield
    SESSION.reset()
    dc.set_terminal_context(False)
    dc._skip_focus_once = False
    dc._focus_delay_s = old_delay


def test_can_you_open_firefox_and_search_for_github() -> None:
    registry = RecordingRegistry()

    assert try_fast_path(registry, "Can you open Firefox and search for GitHub?") == (
        "Searching the web for github in Firefox right now."
    )
    assert registry.calls == [("web_search", {"query": "github", "browser": "firefox"})]


def test_open_the_first_link_please_uses_the_last_query() -> None:
    SESSION.note_search("github", "firefox")
    registry = RecordingRegistry()

    reply = try_fast_path(registry, "Open the first link, please.")

    assert reply == "I've opened the first result for github."
    assert registry.calls == [
        ("open_first_result", {"query": "github", "browser": "firefox"})
    ]


def test_can_you_open_the_first_link_of_github() -> None:
    registry = RecordingRegistry()

    assert try_fast_path(registry, "Can you open the first link of GitHub?") == (
        "I've opened the first result for github."
    )
    assert registry.calls == [("open_first_result", {"query": "github"})]


def test_search_comma_instagram() -> None:
    registry = RecordingRegistry()

    assert try_fast_path(registry, "Search, Instagram") == "Searching the web for instagram in Firefox right now."
    assert registry.calls == [("web_search", {"query": "instagram", "browser": None})]


@pytest.mark.parametrize(
    "text",
    [
        "Go to the new tab",
        "Open the tab",
        "Open a new tab. I'll turn open a new tab in Brave.",
    ],
)
def test_new_tab_phrasings(text: str) -> None:
    registry = RecordingRegistry()

    assert try_fast_path(registry, text) == "I've opened a new tab."
    assert registry.calls == [("browser_tab_control", {"action": "new_tab"})]


def test_new_tab_in_a_named_browser_focuses_it_first() -> None:
    registry = RecordingRegistry()

    reply = try_fast_path(registry, "Open a new tab in Brave")

    assert reply == "I've switched to Brave. I've opened a new tab."
    assert registry.calls == [
        ("switch_app", {"app_name": "brave"}),
        ("browser_tab_control", {"action": "new_tab"}),
    ]


@pytest.mark.parametrize(
    "text",
    ["Okay, close the tab.", "Close the tab Instagram", "shut this tab"],
)
def test_close_tab_phrasings(text: str) -> None:
    registry = RecordingRegistry()

    assert try_fast_path(registry, text) == "I've closed that tab."
    assert registry.calls == [("browser_tab_control", {"action": "close_tab"})]


@pytest.mark.parametrize("text", ["reopen the tab", "restore tab"])
def test_reopen_tab_phrasings(text: str) -> None:
    registry = RecordingRegistry()

    assert try_fast_path(registry, text) == "I've reopened the last tab."
    assert registry.calls == [("browser_tab_control", {"action": "reopen_tab"})]


def test_open_steam_and_discord_from_the_merged_phrase() -> None:
    registry = RecordingRegistry()

    reply = try_fast_path(registry, "Open Steam, Ultron can you open Discord for me?")

    assert reply == "I've opened Steam in Firefox. I've opened Discord in Firefox."
    assert registry.calls == [
        ("open_app", {"app_name": "steam"}),
        ("open_app", {"app_name": "discord"}),
    ]


def test_scroll_down_is_one_page_not_five_arrows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[bool, str]:
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(dc, "_ydotool_runner", runner)
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(dc.scroll_page)

    assert try_fast_path(registry, "Scroll down.") == "I've scrolled the page down."
    assert calls == [["key", "109:1", "109:0"]]


def test_web_app_fallback_opens_the_builtin_url(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[str, str | None]] = []
    monkeypatch.setattr(apps, "find_app", lambda _name: None)
    monkeypatch.setattr(web, "choose_browser", lambda _browser=None: "firefox")
    monkeypatch.setattr(
        web,
        "open_url",
        lambda url, browser=None: opened.append((url, browser))
        or {"ok": True, "opened_with": browser or "the default browser"},
    )

    result = apps.open_app("discord")

    assert result["ok"] is True and result["web_app"] is True
    assert result["url"] == "https://discord.com/app"
    assert result["reply"] == "I've opened Discord in Firefox."
    assert opened == [("https://discord.com/app", "firefox")]
    assert open_app_reply({"ok": True, "raw_summary": result}) == "I've opened Discord in Firefox."


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("discord", "https://discord.com/app"),
        ("instagram", "https://www.instagram.com"),
        ("youtube", "https://www.youtube.com"),
        ("whatsapp", "https://web.whatsapp.com"),
        ("chatgpt", "https://chatgpt.com"),
        ("gmail", "https://mail.google.com"),
        ("github", "https://github.com"),
        ("steam", "https://store.steampowered.com"),
        ("netflix", "https://www.netflix.com"),
        ("spotify", "https://open.spotify.com"),
        ("reddit", "https://www.reddit.com"),
        ("twitter", "https://x.com"),
        ("google", "https://www.google.com"),
        ("claude", "https://claude.ai"),
    ],
)
def test_each_web_app_has_the_expected_url(
    name: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apps, "find_app", lambda _name: None)
    monkeypatch.setattr(web, "choose_browser", lambda _browser=None: "firefox")
    monkeypatch.setattr(web, "open_url", lambda _url, _browser=None: {"ok": True})

    result = apps.open_app(name)

    assert result["ok"] is True and result["url"] == url


def test_local_app_install_wins_over_the_web_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    match = apps.AppMatch(key="discord", display="Discord")
    monkeypatch.setattr(apps, "find_app", lambda _name: match)
    monkeypatch.setattr(apps, "_launch_match", lambda _match: ({"ok": True, "app_name": "Discord"}, ["discord"]))
    monkeypatch.setattr(web, "open_url", lambda *_a, **_k: pytest.fail("web fallback used"))

    result = apps.open_app("discord")

    assert result == {"ok": True, "app_name": "Discord"}


# --------------------------------------------------------------------------------------
# Part 3: one LLM call per tool turn + compact schema + prompt
# --------------------------------------------------------------------------------------


class StubAdapter:
    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, calls: list[tuple[str, dict[str, Any]]], final_text: str = "Done.") -> None:
        self.calls = 0
        self.payloads: list[Any] = []
        self._calls = calls
        self._final_text = final_text

    def select_model(self, role: str = "main") -> str:
        return "b6-stub"

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
        self.payloads.append(tools)
        if self.calls == 1:
            parts = [
                types.Part(function_call=types.FunctionCall(name=name, args=args, id=f"c{index}"))
                for index, (name, args) in enumerate(self._calls)
            ]
            content = types.Content(role="model", parts=parts)
        else:
            content = types.Content(role="model", parts=[types.Part.from_text(text=self._final_text)])
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


def _agent_loop(adapter: Any) -> AgentLoop:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)

    @tool(name="open_app", risk_level="safe", read_only=False)
    def fake_open(app_name: str) -> dict[str, Any]:
        """Open an app."""
        return {"ok": True, "app_name": app_name.title()}

    @tool(name="read_file", risk_level="safe", read_only=True)
    def fake_read(path: str) -> dict[str, Any]:
        """Read a file."""
        return {"ok": True, "path": path, "text": "hello"}

    registry.register(fake_open)
    registry.register(fake_read)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    return AgentLoop(cfg, adapter, registry, watchdog)


def test_agent_loop_makes_one_call_after_open_app() -> None:
    adapter = StubAdapter([("open_app", {"app_name": "firefox"})])

    result = _agent_loop(adapter).run_task("open firefox")

    assert result.reply == "I've opened Firefox for you."
    assert adapter.calls == 1


def test_agent_loop_joins_two_code_replies_without_a_second_call() -> None:
    adapter = StubAdapter(
        [("open_app", {"app_name": "steam"}), ("open_app", {"app_name": "discord"})]
    )

    result = _agent_loop(adapter).run_task("open steam and discord")

    assert result.reply == "I've opened Steam for you. I've opened Discord for you."
    assert adapter.calls == 1


def test_agent_loop_still_calls_twice_for_a_tool_with_no_code_reply() -> None:
    adapter = StubAdapter([("read_file", {"path": "~/x.txt"})], final_text="The file says hello.")

    result = _agent_loop(adapter).run_task("read that file")

    assert result.reply == "The file says hello."
    assert adapter.calls == 2


def test_compact_schema_is_under_380_estimated_tokens() -> None:
    from assistant.tools.apps import close_app, open_app
    from assistant.tools.desktop_control import (
        browser_tab_control,
        media_control,
        press_key,
        scroll_page,
        set_brightness,
        system_info,
        take_screenshot,
        type_text,
        window_control,
    )
    from assistant.tools.files import list_files, read_file
    from assistant.tools.volume import mute_audio, set_volume, volume_down, volume_up
    from assistant.tools.web import open_first_result, web_search

    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe")
    registry = ToolRegistry(config=cfg, gate=gate)
    for fn in (
        list_files,
        read_file,
        open_app,
        close_app,
        volume_up,
        volume_down,
        set_volume,
        mute_audio,
        web_search,
        open_first_result,
        type_text,
        press_key,
        scroll_page,
        browser_tab_control,
        window_control,
        set_brightness,
        media_control,
        system_info,
        take_screenshot,
    ):
        registry.register(fn)

    tools = registry.get_openai_tools(compact=True)
    estimated = len(json.dumps(tools, separators=(",", ":"))) / 4

    assert {item["function"]["name"] for item in tools} == {
        "list_files",
        "read_file",
        "open_app",
        "close_app",
        "volume_up",
        "volume_down",
        "set_volume",
        "mute_audio",
        "web_search",
        "open_first_result",
        "type_text",
        "press_key",
        "scroll_page",
        "browser_tab_control",
        "window_control",
        "set_brightness",
        "media_control",
        "system_info",
        "take_screenshot",
    }
    # Phase 7 exposes every tool; the static prefix is cached, so the extra schemas are
    # paid once rather than on every turn.
    assert estimated <= 1600


def test_local_prompt_has_the_natural_language_examples() -> None:
    prompt = build_system_prompt(load_config(), "auto-safe")

    assert "close_app firefox" in prompt
    assert "open_first_result github" in prompt
    assert "type_text" in prompt
    assert "two open_app calls" in prompt


# --------------------------------------------------------------------------------------
# Part 4: persistent daemon + window focus helpers
# --------------------------------------------------------------------------------------


def test_start_ydotool_is_a_no_op_under_pytest() -> None:
    # The guard keeps pytest from starting a real daemon; production chat/listen still do.
    assert dc.start_ydotool() == ""


def test_ensure_ydotool_starts_once_and_stop_removes_its_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / ".ultron_ydotool.sock"
    starts: list[list[str]] = []

    class FakeProc:
        pid = 4242

        def __init__(self, argv: list[str], **_kwargs: Any) -> None:
            starts.append(argv)
            socket.write_text("", encoding="utf-8")

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    old_proc = dc._ydotoold_proc
    old_registered = dc._atexit_registered
    dc._ydotoold_proc = None
    dc._atexit_registered = False
    monkeypatch.setattr(dc, "_socket_path", lambda: str(socket))
    monkeypatch.setattr(dc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dc.subprocess, "Popen", FakeProc)
    try:
        assert dc.ensure_ydotool() == str(socket)
        assert dc.ensure_ydotool() == str(socket)
        assert starts == [["ydotoold", "--socket-path", str(socket)]]
        dc.stop_ydotool()
        assert not socket.exists()
    finally:
        dc._ydotoold_proc = old_proc
        dc._atexit_registered = old_registered


def test_window_focus_runs_before_scrolling(monkeypatch: pytest.MonkeyPatch) -> None:
    focus_calls: list[list[str]] = []
    key_calls: list[list[str]] = []
    SESSION.note_app("firefox", browser=True)
    dc.set_terminal_context(True)
    dc._focus_delay_s = 0.0
    monkeypatch.setattr(
        dc,
        "_run",
        lambda argv, timeout=5.0: focus_calls.append(list(argv))
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(dc, "_ydotool_runner", lambda argv: key_calls.append(argv) or (True, ""))

    result = dc.scroll_page("down", 1)

    assert result["ok"] is True
    assert focus_calls == [["wmctrl", "-a", "Firefox"]]
    assert key_calls == [["key", "109:1", "109:0"]]


def test_terminal_typing_skips_the_window_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    focus_calls: list[list[str]] = []
    SESSION.note_app("firefox", browser=True)
    dc.set_terminal_context(True)
    dc._focus_delay_s = 0.0
    dc.note_explicit_terminal()
    monkeypatch.setattr(
        dc,
        "_run",
        lambda argv, timeout=5.0: focus_calls.append(list(argv))
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(dc, "_ydotool_runner", lambda _argv: (True, ""))
    monkeypatch.setattr(dc, "_copy_clipboard", lambda _text: True)

    result = dc.type_text("hello")

    assert result["ok"] is True
    assert focus_calls == []


def test_type_text_pastes_the_clipboard_instantly(monkeypatch: pytest.MonkeyPatch) -> None:
    typed: list[list[str]] = []
    copied: list[str] = []
    monkeypatch.setattr(dc, "_ydotool_runner", lambda argv: typed.append(argv) or (True, ""))
    monkeypatch.setattr(dc, "_copy_clipboard", lambda text: copied.append(text) or True)

    result = dc.type_text("hello world")

    assert result["ok"] is True
    assert typed == [["key", "29:1", "47:1", "47:0", "29:0"]]
    assert copied == ["hello world"]
