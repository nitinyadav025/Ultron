"""System doctor (`main.py doctor`), 7-point self-test (`main.py self-test`),
and end-to-end voice diagnostics (`main.py voice-test`) for Fedora 44.
"""

from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path

from assistant.brain.gemini_client import GeminiAdapter
from assistant.brain.server_manager import server_status
from assistant.config import AssistantConfig
from assistant.interfaces.voice import VoiceController
from assistant.safety.killswitch import register_gnome_kill_hotkey
from assistant.safety.permissions import PermissionGate
from assistant.safety.redact import run_redactor_self_test
from assistant.speech.ears import WhisperEars
from assistant.speech.mic_input import apply_mic_source_volume
from assistant.speech.mouth import PiperMouth
from assistant.tools.sandbox import run_sandboxed_script

REQUIRED_BINS = {
    "pip": "python3-pip",
    "git": "git",
    "gcc": "gcc",
    "wl-copy": "wl-clipboard",
    "ydotool": "ydotool",
    "gnome-screenshot": "gnome-screenshot",
    "notify-send": "libnotify",
    "pw-record": "pipewire-utils",
}


def run_doctor(config: AssistantConfig) -> int:
    """Check Fedora 44 packages, Wayland session, and Gemini model IDs."""
    print(f"=== Ultron Doctor (Python {platform.python_version()} | Session: {os.environ.get('XDG_SESSION_TYPE', 'unknown')}) ===")
    missing_pkgs = [pkg for bin_name, pkg in REQUIRED_BINS.items() if not shutil.which(bin_name)]
    if missing_pkgs:
        print(f"[WARN] Missing Fedora packages. Install with:\n  sudo dnf install {' '.join(sorted(set(missing_pkgs)))}")
    else:
        print("[PASS] All core Fedora system binaries found.")

    if config.backend == "local":
        model_ok = Path(config.llama_model_path).exists()
        bin_ok = Path(config.llama_server_binary).exists()
        print(f"[{'PASS' if bin_ok else 'FAIL'}] llama-server binary: {config.llama_server_binary}")
        print(f"[{'PASS' if model_ok else 'FAIL'}] Local model file: {config.llama_model_path}")
        print(f"[INFO] Backend=local ({config.llama_base_url}) — Gemini API checks skipped.")
        ears = WhisperEars(config)
        ears_ok = ears.is_downloaded()
        ears_hint = "" if ears_ok else " (download once: .venv/bin/python main.py download-models)"
        print(f"[{'PASS' if ears_ok else 'FAIL'}] faster-whisper '{config.whisper_model}': {config.whisper_model_dir}{ears_hint}")
        mouth = PiperMouth(config)
        mouth_ok = mouth.is_downloaded()
        mouth_hint = "" if mouth_ok else " (download once: .venv/bin/python main.py download-models)"
        print(f"[{'PASS' if mouth_ok else 'FAIL'}] Piper voice '{config.piper_voice}': {config.piper_voice_dir}{mouth_hint}")
        print("[INFO] Gemini Live voice: disabled on BACKEND=local.")
        state, detail = server_status(config)
        if state == "ready":
            print(f"[PASS] llama-server is running ({detail}).")
        elif state == "loading":
            print(f"[INFO] llama-server is starting ({detail}).")
        elif state == "free":
            print("[INFO] llama-server is not running; `main.py chat`/`gui` starts it automatically.")
        else:
            print(f"[WARN] Port from LLAMA_BASE_URL is not a healthy llama-server: {detail}")
            return 1
        return 0 if (model_ok and bin_ok and ears_ok and mouth_ok) else 1

    if not config.has_valid_key:
        print("[FAIL] GEMINI_API_KEY is missing or placeholder in .env. Copy .env.example to .env and paste your key.")
        return 1

    try:
        adapter = GeminiAdapter(config)
        avail = adapter.list_available_models()
        print(f"[PASS] Connected to Gemini API ({len(avail)} models available on your key).")
        for role, mod_id in config.models.items():
            if not mod_id:
                continue
            status = "FOUND" if mod_id in avail else "NOT LISTED (check model ID in .env)"
            print(f"  - {role}: {mod_id} -> {status}")
        return 0
    except Exception as exc:
        print(f"[FAIL] Gemini API check failed: {exc}")
        return 1


def run_self_test(config: AssistantConfig) -> int:
    """Run all 7 required non-destructive self-tests and print PASS or FAIL."""
    results: list[tuple[str, bool, str]] = []

    results.append(("config loads", bool(config.models.get("main")), f"main={config.current_model}, thinking={config.active_thinking_level}"))
    if config.backend == "local":
        model_path = Path(config.llama_model_path)
        results.append(("backend", True, "BACKEND=local — no Gemini API key required"))
        results.append(("local model present", model_path.exists(), str(model_path)))
    else:
        results.append(("key present", config.has_valid_key, "Valid key in .env" if config.has_valid_key else "Set GEMINI_API_KEY in .env"))
        if config.has_valid_key:
            try:
                avail = GeminiAdapter(config).list_available_models()
                results.append(("model reachable", len(avail) > 0, f"{len(avail)} models reachable"))
            except Exception as exc:
                results.append(("model reachable", False, str(exc)))
        else:
            results.append(("model reachable", False, "Skipped (API key not set in .env)"))

    gate = PermissionGate(config=config, mode="ask", confirm_callback=lambda _d, _t: "no")
    dec = gate.check("delete_file", "confirm", False, {"path": "/etc/passwd"})
    results.append(("permission gate blocks a test deletion", not dec.allowed, dec.reason))

    ok_ks, msg_ks = register_gnome_kill_hotkey(config.kill_hotkey_binding)
    results.append(("kill switch registers", ok_ks, msg_ks))

    ok_red, msg_red = run_redactor_self_test()
    results.append(("redactor catches a planted key", ok_red, msg_red))

    sb = run_sandboxed_script("import time\ntime.sleep(10)\n", filename="selftest_sleep.py", timeout_sec=2)
    results.append(("sandbox runner kills a script that sleeps past its timeout", bool(sb.get("killed_by_timeout")), f"duration={sb.get('duration_sec')}s"))

    all_passed = True
    for name, passed, detail in results:
        tag = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"[{tag}] {name} — {detail}")
    return 0 if all_passed else 1


def _run_local_voice_test(config: AssistantConfig) -> int:
    """Local voice diagnostic: load faster-whisper once, record 3 s, transcribe on the CPU."""
    print("=== Ultron Local Voice Test (faster-whisper, CPU int8) ===")
    voice = VoiceController(config=config, adapter=None)

    print(f"1. Loading model from {config.whisper_model_dir} ...")
    started = time.monotonic()
    ok_load, load_msg = voice.load_ears()
    print(f"[{'PASS' if ok_load else 'FAIL'}] {load_msg} ({time.monotonic() - started:.2f}s)")
    if not ok_load:
        return 1

    print('2. Listening — say: "open firefox and list my downloads"')
    ok_rec, rec_detail = voice.record()
    if not ok_rec:
        print(
            f"[FAIL] Mic recording — {rec_detail} "
            f"(peak={voice.last_peak}, rms={voice.last_rms})"
        )
        return 1
    print(
        f"[PASS] Mic recording — level before peak={voice.last_peak_before}, rms={voice.last_rms_before} "
        f"-> after peak={voice.last_peak}, rms={voice.last_rms}, file={rec_detail}"
    )

    print("3. Transcribing locally ...")
    started = time.monotonic()
    ok_tr, tr_detail = voice.transcribe_audio()
    elapsed = time.monotonic() - started
    if not ok_tr:
        print(f"[FAIL] Transcription ({elapsed:.2f}s) — {tr_detail}")
        return 1
    print(f"[PASS] Transcription ({elapsed:.2f}s) — Heard: {tr_detail!r}")

    print("4. Speaking a sample sentence with Piper ...")
    ok_mouth, mouth_msg = voice.load_mouth()
    print(f"[{'PASS' if ok_mouth else 'FAIL'}] {mouth_msg}")
    if not ok_mouth:
        return 1
    started = time.monotonic()
    ok_say, say_detail = voice.speak_sync("Ultron voice test. Local Piper speech is working.")
    print(f"[{'PASS' if ok_say else 'FAIL'}] Speech ({time.monotonic() - started:.2f}s) — {say_detail}")
    return 0 if ok_say else 1


def run_voice_test(config: AssistantConfig) -> int:
    """Run an interactive voice diagnostic (local ears in local mode, cloud pipeline in gemini mode)."""
    _mic_ok, mic_msg = apply_mic_source_volume(config.mic_source_volume)
    if config.mic_source_volume:
        print(f"[Mic] {mic_msg}")
    if config.backend == "local":
        return _run_local_voice_test(config)
    print("=== Ultron Voice Pipeline Test (Speak into your microphone for 3 seconds) ===")
    adapter = GeminiAdapter(config)
    voice = VoiceController(config=config, adapter=adapter)
    all_ok = True

    # Stage 1: Recording & Mic Level
    print("1. Listening — speak when you see 'Listening...'")
    ok_rec, rec_detail = voice.record()
    if ok_rec:
        print(f"[PASS] Mic recording & level check — peak={voice.last_peak}, rms={voice.last_rms}, file={rec_detail}")
    else:
        print(f"[FAIL] Mic recording & level check — {rec_detail} (peak={voice.last_peak}, rms={voice.last_rms})")
        all_ok = False

    # Stage 2: Cloud Transcription
    if ok_rec:
        print("2. Transcribing recorded audio with Gemini Cloud...")
        ok_tr, tr_detail = voice.transcribe_audio()
        if ok_tr:
            print(f"[PASS] Transcription — Heard: {tr_detail!r}")
        else:
            print(f"[FAIL] Transcription — {tr_detail}")
            all_ok = False
    else:
        print("[FAIL] Transcription — Skipped because stage 1 recording failed.")
        all_ok = False

    # Stage 3: TTS Speech Output
    print("3. Testing TTS speech output...")
    ok_tts, tts_detail = voice.speak_sync("Ultron voice pipeline is online and clear.")
    if ok_tts:
        print(f"[PASS] Speech output — {tts_detail}")
    else:
        print(f"[FAIL] Speech output — {tts_detail}")
        all_ok = False

    return 0 if all_ok else 1
