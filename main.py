#!/usr/bin/env python3
"""Ultron Entry Point: chat | gui | doctor | self-test | voice-test | logs | stop | download-models."""

from __future__ import annotations

import argparse
import sys

from assistant.brain.server_manager import LlamaServerError, LlamaServerManager
from assistant.config import AssistantConfig, load_config
from assistant.doctor import run_doctor, run_self_test, run_voice_test
from assistant.interfaces.cli import run_cli_chat
from assistant.interfaces.gui import run_gui_dashboard
from assistant.safety.audit import read_audit_logs
from assistant.safety.killswitch import trigger_kill_switch

# Commands that need the local LLM, so Phase 2 may auto-start llama-server for them.
BRAIN_COMMANDS = ("chat", "gui", "listen")


def _server_progress(message: str) -> None:
    print(f"[llama-server] {message}", flush=True)


def _download_models(config: AssistantConfig) -> int:
    """One-time download of the local speech models into the project (works offline afterwards)."""
    from assistant.speech.ears import download_whisper_model
    from assistant.speech.mouth import download_piper_voice

    try:
        whisper_path = download_whisper_model(config, progress=print)
        piper_path = download_piper_voice(config, progress=print)
    except Exception as exc:
        print(f"[ERROR] Download failed: {type(exc).__name__}: {exc}")
        print("        Wi-Fi is needed for this one-time step only.")
        return 1
    print(f"Done. Ultron now runs offline with:\n  ears : {whisper_path}\n  mouth: {piper_path}")
    return 0


def _dispatch(args: argparse.Namespace, config: AssistantConfig) -> int:
    if args.command == "stop":
        res = trigger_kill_switch("Manual `python3 main.py stop` invoked")
        print(f"[KILL SWITCH TRIGGERED] {res}")
        return 0
    if args.command == "download-models":
        return _download_models(config)
    if args.command == "teach":
        from assistant.brain.skills import configure_runtime, save_user_skill, split_actions

        if len(args.args) < 2:
            print('Usage: main.py teach "<trigger>" "<action1>; <action2>"')
            return 2
        configure_runtime(config)
        try:
            skill = save_user_skill(config, args.args[0], split_actions(args.args[1]))
        except (OSError, ValueError) as exc:
            print(f"[ERROR] {exc}")
            return 1
        print(f"Learned: {skill.trigger} -> {'; '.join(skill.actions)}")
        return 0
    if args.command == "import-skills":
        from assistant.brain.skills import configure_runtime, import_skills_file

        if len(args.args) < 1:
            print("Usage: main.py import-skills <path.json>")
            return 2
        configure_runtime(config)
        try:
            imported = import_skills_file(config, args.args[0])
        except (OSError, ValueError) as exc:
            print(f"[ERROR] {exc}")
            return 1
        print(f"Imported {len(imported)} skill(s) into {config.user_skills_file}")
        return 0
    if args.command == "benchmark":
        from scripts.benchmark_phase5c import run_benchmark

        return 0 if run_benchmark()["failed"] == 0 else 1
    if args.command == "doctor":
        return run_doctor(config)
    if args.command == "self-test":
        return run_self_test(config)
    if args.command == "voice-test":
        return run_voice_test(config)
    if args.command == "listen":
        from assistant.interfaces.listen import run_listen

        return run_listen(config)
    if args.command == "wake-train":
        from assistant.interfaces.wake_train import run_wake_train

        return run_wake_train(config)
    if args.command == "logs":
        for item in read_audit_logs(30):
            print(item)
        return 0
    if args.command == "gui":
        return run_gui_dashboard(config)
    return run_cli_chat(config, initial_mode=args.mode, dry_run_default=args.dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ultron — Fedora 44 local voice assistant")
    parser.add_argument(
        "command",
        nargs="?",
        default="chat",
        choices=[
            "chat",
            "gui",
            "listen",
            "wake-train",
            "benchmark",
            "doctor",
            "self-test",
            "voice-test",
            "logs",
            "stop",
            "download-models",
            "teach",
            "import-skills",
        ],
    )
    parser.add_argument("args", nargs="*", help="teach: trigger and actions; import-skills: path")
    parser.add_argument("--mode", choices=["ask", "auto-safe", "human-only"], default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()

    # Phase 2: make sure the local brain is up before the chat/GUI loop starts.
    server: LlamaServerManager | None = None
    if config.backend == "local" and args.command in BRAIN_COMMANDS:
        server = LlamaServerManager(config)
        try:
            server.ensure_running(progress=_server_progress)
        except LlamaServerError as exc:
            print(f"[ERROR] {exc}")
            server.close()
            return 1
        except KeyboardInterrupt:
            print("\n[llama-server] Startup cancelled; child process stopped.")
            server.close()
            return 130

    try:
        return _dispatch(args, config)
    finally:
        if server is not None:
            server.close()


if __name__ == "__main__":
    sys.exit(main())
