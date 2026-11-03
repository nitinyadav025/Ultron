"""Ultron Dashboard (GUI) for Fedora 44 GNOME 50.5 Wayland.
Includes live Model Switcher, Thinking Level Switcher (low/medium/high), Safety Mode selector,
Voice Push-to-Talk button (Listening -> Transcribing -> Thinking -> Speaking -> Ready),
and always-visible Emergency STOP button.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from assistant.brain import build_adapter
from assistant.brain.agent_loop import AgentLoop
from assistant.config import VALID_THINKING_LEVELS, AssistantConfig
from assistant.brain.skills import configure_runtime, learn_skill
from assistant.interfaces.voice import VoiceController
from assistant.safety.killswitch import WatchdogMonitor, register_gnome_kill_hotkey, trigger_kill_switch
from assistant.safety.permissions import VALID_MODES, PermissionGate
from assistant.tools.apps import close_app, open_app, switch_app
from assistant.tools.desktop_control import (
    browser_tab_control,
    media_control,
    press_key,
    scroll_page,
    set_brightness,
    set_timer,
    set_timer_speaker,
    system_info,
    take_screenshot,
    type_text,
    window_control,
)
from assistant.tools.files import list_files, read_file
from assistant.tools.registry import ToolRegistry
from assistant.tools.volume import mute_audio, set_volume, volume_down, volume_up


def run_gui_dashboard(config: AssistantConfig) -> int:
    """Launch the Ultron Dashboard window."""
    configure_runtime(config)
    register_gnome_kill_hotkey(config.kill_hotkey_binding)
    root = tk.Tk()
    root.title(f"{config.assistant_name} Dashboard — Fedora 44 (Wayland)")
    root.geometry("860x600")
    root.configure(bg="#12161f")

    gate = PermissionGate(config=config, mode=config.default_mode)
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
    ):
        registry.register(fn)

    watchdog = WatchdogMonitor(config.max_steps, config.wall_clock_timeout_sec, config.idle_standdown_sec)
    adapter = build_adapter(config)

    def _ready_text() -> str:
        engine_note = f" | {voice.active_tts_engine}" if voice.active_tts_engine else ""
        return f"Ready | Model: {config.current_model} | Thinking: {config.active_thinking_level}{engine_note}"

    status_var = tk.StringVar(value=f"Ready | Model: {config.current_model} | Thinking: {config.active_thinking_level}")
    usage_var = tk.StringVar(value=f"Today's Tokens: {adapter.usage_meter.get_today_total()} / {config.daily_token_cap}")
    dry_run_var = tk.BooleanVar(value=False)
    speak_reply_var = tk.BooleanVar(value=True)

    voice = VoiceController(
        config=config,
        adapter=adapter,
        status_callback=lambda msg: root.after(0, status_var.set, msg),
    )
    ears_ok, ears_msg = voice.load_ears() if voice.ears is not None else (False, "")
    mouth_ok, mouth_msg = voice.load_mouth() if voice.mouth is not None else (False, "")

    def gui_confirm(detail: str, _timeout: int) -> str:
        ans = messagebox.askyesno("Ultron Permission Gate", f"Approve exact action?\n\n{detail}")
        return "yes" if ans else "no"

    gate.confirm_callback = gui_confirm
    set_timer_speaker(lambda text: voice.speak_sync(text))
    loop = AgentLoop(config, adapter, registry, watchdog, step_callback=lambda s: root.after(0, status_var.set, s))

    # Top Control Bar (Model Switcher, Thinking Level Switcher, Mode Switcher)
    top = tk.Frame(root, bg="#1b2230", padx=10, pady=8)
    top.pack(fill=tk.X)

    tk.Label(top, text="Model:", bg="#1b2230", fg="#e2e8f0").pack(side=tk.LEFT)
    model_var = tk.StringVar(value=config.current_model)
    model_cb = ttk.Combobox(top, textvariable=model_var, values=config.available_model_choices(), width=22, state="readonly")
    model_cb.pack(side=tk.LEFT, padx=(4, 12))

    tk.Label(top, text="Thinking:", bg="#1b2230", fg="#e2e8f0").pack(side=tk.LEFT)
    think_var = tk.StringVar(value=config.active_thinking_level)
    think_cb = ttk.Combobox(top, textvariable=think_var, values=list(VALID_THINKING_LEVELS), width=9, state="readonly")
    think_cb.pack(side=tk.LEFT, padx=(4, 12))

    tk.Label(top, text="Mode:", bg="#1b2230", fg="#e2e8f0").pack(side=tk.LEFT)
    mode_var = tk.StringVar(value=gate.mode)
    mode_cb = ttk.Combobox(top, textvariable=mode_var, values=list(VALID_MODES), width=11, state="readonly")
    mode_cb.pack(side=tk.LEFT, padx=(4, 12))

    tk.Checkbutton(top, text="Dry Run", variable=dry_run_var, bg="#1b2230", fg="#e2e8f0", selectcolor="#12161f").pack(side=tk.LEFT, padx=4)
    tk.Checkbutton(top, text="Voice TTS", variable=speak_reply_var, bg="#1b2230", fg="#e2e8f0", selectcolor="#12161f").pack(side=tk.LEFT, padx=4)

    def _on_settings_change(_event: object = None) -> None:
        config.set_active_model(model_var.get())
        config.set_thinking_level(think_var.get())
        gate.set_mode(mode_var.get())
        status_var.set(_ready_text())

    model_cb.bind("<<ComboboxSelected>>", _on_settings_change)
    think_cb.bind("<<ComboboxSelected>>", _on_settings_change)
    mode_cb.bind("<<ComboboxSelected>>", _on_settings_change)

    # Chat transcript
    chat_box = tk.Text(root, bg="#0f131a", fg="#f1f5f9", font=("Monospace", 10), wrap=tk.WORD, padx=10, pady=10)
    chat_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

    def append_chat(speaker: str, text: str) -> None:
        chat_box.insert(tk.END, f"{speaker}: {text}\n\n")
        chat_box.see(tk.END)

    # Bottom Input + Voice + Pause/Resume + Emergency STOP
    bottom = tk.Frame(root, bg="#1b2230", padx=10, pady=8)
    bottom.pack(fill=tk.X)
    entry = tk.Entry(bottom, bg="#0f131a", fg="#ffffff", insertbackground="#ffffff", font=("Sans", 11))
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

    speak_btn = tk.Button(bottom, text="🎙️ Speak (5s)", bg="#059669", fg="#ffffff")

    def _run_task_and_speak(prompt_text: str, speaker_label: str = "You", force_speak: bool = False) -> None:
        """Run the prompt through loop.run_task() and optionally speak the reply."""
        try:
            root.after(0, append_chat, speaker_label, prompt_text)
            root.after(0, status_var.set, "Thinking...")
            res = loop.run_task(prompt_text, dry_run=dry_run_var.get())
            root.after(0, append_chat, config.assistant_name, res.reply)
            root.after(
                0,
                usage_var.set,
                f"Task: {res.usage.total_tokens} tok | Today: {res.usage.daily_total}/{res.usage.daily_cap} ({res.usage.cap_percent}%)",
            )
            if (speak_reply_var.get() or force_speak) and res.ok and res.reply:
                root.after(0, status_var.set, "Speaking...")
                ok_s, msg_s = voice.speak_sync(res.reply)
                if not ok_s:
                    root.after(0, append_chat, "Voice", msg_s)
        except Exception as exc:
            root.after(0, append_chat, "Error", str(exc))

    def _execute_prompt(prompt_text: str) -> None:
        if not prompt_text.strip():
            return
        voice.stop_speaking()

        def _worker() -> None:
            root.after(0, lambda: speak_btn.configure(state=tk.DISABLED))
            try:
                _run_task_and_speak(prompt_text, speaker_label="You", force_speak=False)
            finally:
                root.after(0, lambda: speak_btn.configure(state=tk.NORMAL))
                root.after(0, lambda: status_var.set(_ready_text()))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_send(_event: object = None) -> None:
        txt = entry.get().strip()
        entry.delete(0, tk.END)
        _execute_prompt(txt)

    def _on_voice_ptt() -> None:
        voice.stop_speaking()
        speak_btn.configure(state=tk.DISABLED)
        status_var.set("Listening...")

        def _vworker() -> None:
            try:
                ok_rec, rec_msg = voice.record()
                if not ok_rec:
                    root.after(0, append_chat, "Voice", rec_msg)
                    return

                root.after(0, status_var.set, "Transcribing...")
                ok_tr, transcript_or_err = voice.transcribe_audio()
                if not ok_tr:
                    root.after(0, append_chat, "Voice", transcript_or_err)
                    return

                _run_task_and_speak(transcript_or_err, speaker_label="You (voice)", force_speak=True)
            except Exception as exc:
                root.after(0, append_chat, "Voice", f"{type(exc).__name__}: {exc}")
            finally:
                root.after(0, lambda: speak_btn.configure(state=tk.NORMAL))
                root.after(0, lambda: status_var.set(_ready_text()))

        threading.Thread(target=_vworker, daemon=True).start()

    def _on_stop() -> None:
        voice.stop_speaking()
        trigger_kill_switch("GUI Emergency STOP button pressed")
        status_var.set(_ready_text())

    entry.bind("<Return>", _on_send)
    speak_btn.configure(command=_on_voice_ptt)
    tk.Button(bottom, text="Send", bg="#2563eb", fg="#ffffff", command=_on_send).pack(side=tk.LEFT, padx=3)
    speak_btn.pack(side=tk.LEFT, padx=3)
    tk.Button(bottom, text="Pause", bg="#475569", fg="#ffffff", command=loop.pause).pack(side=tk.LEFT, padx=3)
    tk.Button(bottom, text="Resume", bg="#475569", fg="#ffffff", command=loop.resume).pack(side=tk.LEFT, padx=3)
    tk.Button(bottom, text="STOP (Kill)", bg="#dc2626", fg="#ffffff", command=_on_stop).pack(side=tk.LEFT, padx=3)

    footer = tk.Frame(root, bg="#12161f", padx=10, pady=4)
    footer.pack(fill=tk.X)
    tk.Label(footer, textvariable=status_var, bg="#12161f", fg="#38bdf8").pack(side=tk.LEFT)
    tk.Label(footer, textvariable=usage_var, bg="#12161f", fg="#94a3b8").pack(side=tk.RIGHT)

    append_chat(config.assistant_name, "Dashboard ready. Switch Models & Thinking Levels above, type a task, or click '🎙️ Speak (5s)'.")
    if voice.ears is not None:
        append_chat("Ears", f"{'ready' if ears_ok else 'unavailable'} — {ears_msg}")
    if voice.mouth is not None:
        append_chat("Mouth", f"{'ready' if mouth_ok else 'unavailable'} — {mouth_msg}")
    root.mainloop()
    return 0
