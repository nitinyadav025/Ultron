"""Interactive CLI Chat with live Model/Thinking switching (/model, /thinking) and Voice (/voice)."""

from __future__ import annotations

from assistant.brain import build_adapter
from assistant.brain.agent_loop import AgentLoop
from assistant.brain.gemini_client import TokenUsageStats
from assistant.config import LOGS_DIR, AssistantConfig
from assistant.interfaces.voice import VoiceController
from assistant.safety.audit import read_audit_logs
from assistant.safety.killswitch import WatchdogMonitor, register_gnome_kill_hotkey, trigger_kill_switch
from assistant.safety.permissions import PermissionGate
from assistant.tools.apps import open_app
from assistant.tools.files import list_files, read_file
from assistant.tools.registry import ToolRegistry
from assistant.tools.volume import mute_audio, set_volume, volume_down, volume_up

PRIVACY_ACK_FILE = LOGS_DIR / ".privacy_notice_shown"


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
    register_gnome_kill_hotkey(config.kill_hotkey_binding)

    gate = PermissionGate(config=config, mode=initial_mode or config.default_mode)
    registry = ToolRegistry(config=config, gate=gate)
    for fn in (list_files, read_file, open_app, volume_up, volume_down, set_volume, mute_audio):
        registry.register(fn)

    watchdog = WatchdogMonitor(config.max_steps, config.wall_clock_timeout_sec, config.idle_standdown_sec)
    adapter = build_adapter(config)
    voice = VoiceController(config=config, adapter=adapter, status_callback=lambda s: print(f"  -> {s}", flush=True))
    if voice.ears is not None:
        print("  Loading local ears (faster-whisper, CPU int8)...", flush=True)
        ok_ears, ears_msg = voice.load_ears()
        print(f"  Ears: {'ready' if ok_ears else 'unavailable'} — {ears_msg}", flush=True)
    if voice.mouth is not None:
        print("  Loading local mouth (Piper, CPU)...", flush=True)
        ok_mouth, mouth_msg = voice.load_mouth()
        print(f"  Mouth: {'ready' if ok_mouth else 'unavailable'} — {mouth_msg}", flush=True)
    loop = AgentLoop(config, adapter, registry, watchdog, step_callback=lambda s: print(f"  -> {s}", flush=True))
    dry_run = dry_run_default

    print(f"{config.assistant_name} ready on Fedora 44. Model: [{config.current_model}] | Thinking: [{config.active_thinking_level}] | Mode: [{gate.mode}]")
    print("Commands: /model <id> | /thinking <low|medium|high> | /voice | /mode <ask|auto-safe|human-only> | /dry-run | /logs | /quit\n")

    while True:
        try:
            user_in = input(f"You ({config.current_model}/{config.active_thinking_level})> ").strip()
        except (EOFError, KeyboardInterrupt):
            voice.stop_speaking()
            print("\nExiting Ultron.")
            return 0

        if not user_in:
            continue
        if user_in in ("/quit", "/exit", "exit", "quit"):
            voice.stop_speaking()
            return 0
        if user_in.startswith("/model"):
            parts = user_in.split(maxsplit=1)
            if len(parts) == 1:
                print(f"Available models: {config.available_model_choices()} (current: {config.current_model})")
            else:
                try:
                    print(f"Active model switched to: {config.set_active_model(parts[1])}")
                except ValueError as exc:
                    print(f"Error: {exc}")
            continue
        if user_in.startswith("/thinking"):
            parts = user_in.split(maxsplit=1)
            if len(parts) == 1:
                print(f"Current thinking level: {config.active_thinking_level} (choices: low, medium, high)")
            else:
                try:
                    print(f"Thinking level switched to: {config.set_thinking_level(parts[1])}")
                except ValueError as exc:
                    print(f"Error: {exc}")
            continue
        if user_in.startswith("/mode"):
            parts = user_in.split(maxsplit=1)
            if len(parts) > 1:
                try:
                    print(f"Safety mode switched to: {gate.set_mode(parts[1])}")
                except ValueError as exc:
                    print(f"Error: {exc}")
            continue
        if user_in == "/dry-run":
            dry_run = not dry_run
            print(f"Dry-run mode: {dry_run}")
            continue
        if user_in == "/logs":
            for entry in read_audit_logs(10):
                print(f"  {entry['time']} | {entry['tool']} | approved={entry['approved']}")
            continue
        if user_in == "/stop":
            voice.stop_speaking()
            trigger_kill_switch("CLI /stop command")
            continue

        is_voice_turn = False
        if user_in == "/voice":
            ok_rec, rec_msg = voice.record()
            if not ok_rec:
                print(f"Voice: {rec_msg}\n")
                continue
            print("  -> Transcribing...")
            ok_tr, transcript = voice.transcribe_audio()
            if not ok_tr:
                print(f"Voice: {transcript}\n")
                continue
            print(
                f"  -> Mic level: peak {voice.last_peak_before} rms {voice.last_rms_before} "
                f"-> peak {voice.last_peak} rms {voice.last_rms}"
            )
            print(f"You (voice): {transcript}")
            user_in = transcript
            is_voice_turn = True

        try:
            res = loop.run_task(user_in, dry_run=dry_run)
            print(f"\n{config.assistant_name}: {res.reply}")
            u: TokenUsageStats = res.usage
            print(f"  [Usage] Task={u.total_tokens} tok | Today={u.daily_total}/{u.daily_cap} ({u.cap_percent}%)\n")
            if is_voice_turn and res.ok and res.reply:
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
