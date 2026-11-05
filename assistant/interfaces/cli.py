"""Interactive CLI Chat with live Model/Thinking switching (/model, /thinking) and Voice (/voice)."""

from __future__ import annotations

import logging
import re

from assistant.brain import build_adapter
from assistant.brain.agent_loop import AgentLoop
from assistant.brain.context import SESSION, ensure_accessibility
from assistant.brain.fast_path import match_fast_command, try_fast_path
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.skills import configure_runtime, learn_skill
from assistant.config import LOGS_DIR, AssistantConfig
from assistant.interfaces.voice import NOTHING_HEARD, VoiceController
from assistant.safety.audit import read_audit_logs
from assistant.safety.killswitch import WatchdogMonitor, register_gnome_kill_hotkey, trigger_kill_switch
from assistant.safety.permissions import PermissionGate
from assistant.safety.redact import redact_secrets
from assistant.speech.ears import UNCLEAR_SPEECH
from assistant.speech.mic_input import apply_mic_source_volume
from assistant.speech.vad import get_shared_vad
from assistant.tools.apps import close_app, open_app, switch_app
from assistant.tools.desktop_control import (
    browser_tab_control,
    media_control,
    press_key,
    scroll_page,
    set_brightness,
    set_terminal_context,
    set_timer,
    set_timer_speaker,
    start_ydotool,
    system_info,
    take_screenshot,
    type_text,
    window_control,
)
from assistant.tools.files import list_files, read_file
from assistant.tools.registry import ToolRegistry
from assistant.tools.screen_actions import click_element, focus_and_type, read_screen
from assistant.tools.volume import mute_audio, set_volume, volume_down, volume_up
from assistant.tools.web import open_first_result, web_search
from assistant.vision.screen_watcher import start_screen_watcher

PRIVACY_ACK_FILE = LOGS_DIR / ".privacy_notice_shown"

logger = logging.getLogger("ultron.cli")

KNOWN_COMMANDS = frozenset(
    {"/model", "/thinking", "/voice", "/mode", "/dry-run", "/logs", "/quit", "/exit", "/stop"}
)
UNKNOWN_COMMAND_MESSAGE = (
    "Unknown command. Available: /model, /thinking, /voice, /mode, /dry-run, /logs, /quit"
)
_BARE_LOG_WORDS = frozenset({"voice", "quit", "logs"})


def normalize_chat_line(line: str) -> str:
    """Trim a typed line and collapse repeated leading slashes ("//voice" -> "/voice")."""
    cleaned = (line or "").strip()
    return re.sub(r"^/+", "/", cleaned)


def command_name(line: str) -> str:
    """Return "/voice" for "/voice now", and "" for ordinary text."""
    if not line.startswith("/"):
        return ""
    return line.split(maxsplit=1)[0]


def is_unknown_command(line: str) -> bool:
    """True for a slash line whose command name Ultron does not implement."""
    name = command_name(line)
    return bool(name) and name not in KNOWN_COMMANDS


def show_and_speak_outcome(voice: VoiceController, message: str) -> None:
    """Show a voice outcome and speak it exactly once (Ctrl+C stops only the speech)."""
    print(f"Voice: {message}\n")
    try:
        ok, detail = voice.speak_sync(message)
        if not ok:
            print(f"Voice: could not speak that ({detail})\n")
    except KeyboardInterrupt:
        voice.stop_speaking()


def show_privacy_notice_once(config: AssistantConfig) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if PRIVACY_ACK_FILE.exists():
        return
    tier = "PAID tier (not used to train Google products)" if config.paid_tier else "FREE tier (content may be used by Google to improve products)"
    print(f"=== ULTRON PRIVACY NOTICE ===\nPrompts & tool results go to the Gemini API ({tier}). Secrets are redacted. Kill hotkey: Ctrl+Alt+Q.\n")
    PRIVACY_ACK_FILE.write_text("shown\n", encoding="utf-8")


def run_cli_chat(config: AssistantConfig, initial_mode: str | None = None, dry_run_default: bool = False) -> int:
    """Start the interactive CLI chat loop."""
    show_privacy_notice_once(config)
    configure_runtime(config)
    register_gnome_kill_hotkey(config.kill_hotkey_binding)
    _mic_ok, mic_msg = apply_mic_source_volume(config.mic_source_volume)
    if config.mic_source_volume:
        print(f"  Mic input: {mic_msg}", flush=True)
    # One keyboard daemon for the whole session, and focus the last app before key actions.
    start_ydotool()
    set_terminal_context(True)
    # Native Wayland windows are invisible to wmctrl until GNOME introspection is enabled.
    # ensure_accessibility also publishes GTK/Qt/Electron widgets on the AT-SPI bus.
    ensure_accessibility()
    # Phase 8 eyes: refresh the @ref snapshot every 1.2 s in the background.
    start_screen_watcher(config.screen_watch_interval)

    gate = PermissionGate(config=config, mode=initial_mode or config.default_mode)
    registry = ToolRegistry(config=config, gate=gate)
    for fn in (
        list_files,
        read_file,
        open_app,
        close_app,
        switch_app,
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
        set_timer,
        learn_skill,
        click_element,
        read_screen,
        focus_and_type,
    ):
        registry.register(fn)

    watchdog = WatchdogMonitor(config.max_steps, config.wall_clock_timeout_sec, config.idle_standdown_sec)
    adapter = build_adapter(config)
    voice = VoiceController(
        config=config,
        adapter=adapter,
        status_callback=lambda s: print(f"  -> {s}", flush=True),
        vad=get_shared_vad(),
    )
    if voice.ears is not None:
        print("  Loading local ears (faster-whisper, CPU int8)...", flush=True)
        ok_ears, ears_msg = voice.load_ears()
        print(f"  Ears: {'ready' if ok_ears else 'unavailable'} — {ears_msg}", flush=True)
    if voice.mouth is not None:
        print("  Loading local mouth (Piper, CPU)...", flush=True)
        ok_mouth, mouth_msg = voice.load_mouth()
        print(f"  Mouth: {'ready' if ok_mouth else 'unavailable'} — {mouth_msg}", flush=True)
    loop = AgentLoop(config, adapter, registry, watchdog, step_callback=lambda s: print(f"  -> {s}", flush=True))
    set_timer_speaker(lambda text: voice.speak_sync(text))
    dry_run = dry_run_default

    print(f"{config.assistant_name} ready on Fedora 44. Model: [{config.current_model}] | Thinking: [{config.active_thinking_level}] | Mode: [{gate.mode}]")
    print("Commands: /model <id> | /thinking <low|medium|high> | /voice | /mode <ask|auto-safe|human-only> | /dry-run | /logs | /quit\n")

    while True:
        try:
            raw_line = input(f"You ({config.current_model}/{config.active_thinking_level})> ")
        except (EOFError, KeyboardInterrupt):
            voice.stop_speaking()
            print("\nExiting Ultron.")
            return 0

        user_in = normalize_chat_line(raw_line)
        plain = raw_line.strip()
        if plain.startswith("/") or plain.lower() in _BARE_LOG_WORDS:
            logger.info("Chat input: %r", redact_secrets(raw_line))

        if not user_in:
            continue
        name = command_name(user_in)
        if name in ("/quit", "/exit") or user_in in ("exit", "quit"):
            voice.stop_speaking()
            return 0
        if name == "/model":
            parts = user_in.split(maxsplit=1)
            if len(parts) == 1:
                print(f"Available models: {config.available_model_choices()} (current: {config.current_model})")
            else:
                try:
                    print(f"Active model switched to: {config.set_active_model(parts[1])}")
                except ValueError as exc:
                    print(f"Error: {exc}")
            continue
        if name == "/thinking":
            parts = user_in.split(maxsplit=1)
            if len(parts) == 1:
                print(f"Current thinking level: {config.active_thinking_level} (choices: low, medium, high)")
            else:
                try:
                    print(f"Thinking level switched to: {config.set_thinking_level(parts[1])}")
                except ValueError as exc:
                    print(f"Error: {exc}")
            continue
        if name == "/mode":
            parts = user_in.split(maxsplit=1)
            if len(parts) > 1:
                try:
                    print(f"Safety mode switched to: {gate.set_mode(parts[1])}")
                except ValueError as exc:
                    print(f"Error: {exc}")
            continue
        if name == "/dry-run":
            dry_run = not dry_run
            print(f"Dry-run mode: {dry_run}")
            continue
        if name == "/logs":
            for entry in read_audit_logs(10):
                print(f"  {entry['time']} | {entry['tool']} | approved={entry['approved']}")
            continue
        if name == "/stop":
            voice.stop_speaking()
            trigger_kill_switch("CLI /stop command")
            continue

        is_voice_turn = False
        if name == "/voice":
            ok_rec, rec_msg = voice.record()
            if not ok_rec:
                if rec_msg == NOTHING_HEARD:
                    # "Nothing heard" is an outcome too: show it AND speak it.
                    show_and_speak_outcome(voice, rec_msg)
                else:
                    print(f"Voice: {rec_msg}\n")
                continue
            print("  -> Transcribing...")
            ok_tr, transcript = voice.transcribe_audio()
            if not ok_tr:
                # Voice-only failures are spoken so the user is never left without feedback.
                if transcript in (UNCLEAR_SPEECH, NOTHING_HEARD):
                    show_and_speak_outcome(voice, transcript)
                else:
                    print(f"Voice: {transcript}\n")
                continue
            print(
                f"  -> Mic level: peak {voice.last_peak_before} rms {voice.last_rms_before} "
                f"-> peak {voice.last_peak} rms {voice.last_rms}"
            )
            print(f"You (voice): {transcript}")
            user_in = transcript
            is_voice_turn = True
        elif name:
            # A slash line is always a command. Never let it reach the LLM by accident.
            print(UNKNOWN_COMMAND_MESSAGE)
            continue

        if is_voice_turn and getattr(config, "brain_replies", "auto") != "llm":
            fast_reply = try_fast_path(registry, user_in)
            if fast_reply is not None:
                print(f"\n{config.assistant_name}: {fast_reply}\n")
                info = match_fast_command(user_in) or {}
                record_turn = getattr(loop, "record_external_turn", None)
                if callable(record_turn):
                    record_turn(
                        user_in,
                        str(info.get("tool") or ""),
                        dict(info.get("arguments") or {}),
                        fast_reply,
                    )
                SESSION.note_turn(user_in, fast_reply)
                try:
                    ok_s, msg_s = voice.speak_sync(fast_reply)
                    if not ok_s:
                        print(f"Voice: could not speak that ({msg_s})\n")
                except KeyboardInterrupt:
                    voice.stop_speaking()
                continue

        try:
            res = loop.run_task(user_in, dry_run=dry_run)
            print(f"\n{config.assistant_name}: {res.reply}")
            SESSION.note_turn(user_in, res.reply)
            u: TokenUsageStats = res.usage
            print(f"  [Usage] Task={u.total_tokens} tok | Today={u.daily_total}/{u.daily_cap} ({u.cap_percent}%)\n")
            if is_voice_turn and res.reply:
                try:
                    ok_s, msg_s = voice.speak_sync(res.reply)
                    if not ok_s:
                        print(f"Voice: {msg_s}\n")
                except KeyboardInterrupt:
                    # Ctrl+C while Ultron is talking: stop the voice, keep the session alive.
                    voice.stop_speaking()
                    print("\n  (speech interrupted — press Ctrl+C again at the prompt to exit)")
                    continue
        except Exception as exc:
            print(f"\n[Error] {exc}\n")
