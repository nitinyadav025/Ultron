"""One-click standalone launcher for Ultron's living environment.

Starts llama-server, ydotool, the screen watcher, telemetry, the managed voice controller
and the local HTTP/SSE server, then opens the Dashboard in a chromeless app window unless
``--headless`` was requested. Every component is stopped cleanly on SIGINT/SIGTERM.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Any

import psutil

from assistant import os_release_name
from assistant.brain import build_adapter
from assistant.brain.agent_loop import AgentLoop
from assistant.brain.context import ensure_accessibility
from assistant.brain.fast_path import match_fast_command, run_fast_command
from assistant.brain.prompts import build_chat_prompt
from assistant.brain.server_manager import LlamaServerError, LlamaServerManager
from assistant.config import AssistantConfig
from assistant.environment.browser_bridge import get_browser_bridge
from assistant.environment.server import DEFAULT_PORT, EnvironmentServer
from assistant.environment.state import EnvironmentState, get_state
from assistant.environment.voice_controller import EnvironmentVoiceController
from assistant.genai_types import types
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.speech.ears import WhisperEars
from assistant.speech.mouth import PiperMouth
from assistant.speech.vad import get_shared_vad
from assistant.tools.apps import close_app, open_app, switch_app
from assistant.tools.browser_tabs import interact_with_tab, list_browser_tabs, switch_to_tab
from assistant.tools.desktop_control import (
    browser_tab_control,
    media_control,
    press_key,
    scroll_page,
    set_brightness,
    set_timer,
    set_timer_speaker,
    start_ydotool,
    stop_ydotool,
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
from assistant.vision.screen_watcher import start_screen_watcher, stop_screen_watcher

logger = logging.getLogger("ultron.environment.launcher")

TOOL_FUNCTIONS = (
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
    click_element,
    read_screen,
    focus_and_type,
    list_browser_tabs,
    switch_to_tab,
    interact_with_tab,
)
try:  # learn_skill lives with the skills engine
    from assistant.brain.skills import learn_skill as _learn_skill

    TOOL_FUNCTIONS += (_learn_skill,)
except Exception:  # pragma: no cover - the tool is optional for the environment
    pass


class TelemetryCollector(threading.Thread):
    """Pushes GPU/CPU/RAM/llama-server telemetry into the shared state every 2 s."""

    def __init__(self, state: EnvironmentState, interval: float = 2.0) -> None:
        super().__init__(name="ultron-telemetry", daemon=True)
        self.state = state
        self.interval = max(0.5, float(interval))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception as exc:  # telemetry must never kill the environment
                logger.debug("Telemetry sample failed: %s", type(exc).__name__)
            self._stop.wait(self.interval)

    def _sample(self) -> None:
        used_mb = total_mb = util = 0
        if shutil.which("nvidia-smi"):
            try:
                done = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used,memory.total,utilization.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                    check=False,
                )
                parts = [part.strip() for part in done.stdout.strip().split(",")]
                if len(parts) >= 3:
                    used_mb, total_mb, util = int(float(parts[0])), int(float(parts[1])), float(parts[2])
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        memory = psutil.virtual_memory()
        llama_alive = False
        try:
            from assistant.brain.server_manager import port_is_open

            llama_alive = bool(port_is_open("127.0.0.1", 9931))
        except Exception:
            pass
        # The ScreenWatcher keeps SESSION fresh; mirror it into the Dashboard state.
        try:
            from assistant.brain.context import SESSION

            self.state.update_screen(
                focused_window=SESSION.focused_window,
                focused_app=SESSION.focused_app,
                active_site=SESSION.active_site,
                open_windows=SESSION.open_windows,
                visible_elements=SESSION.screen_elements,
            )
        except Exception:
            pass
        self.state.update_telemetry(
            gpu_vram_used_mb=used_mb,
            gpu_vram_total_mb=total_mb or 4096,
            gpu_util_pct=util,
            cpu_pct=float(psutil.cpu_percent(interval=None)),
            ram_used_mb=int(memory.used // (1024 * 1024)),
            llama_server_alive=llama_alive,
        )


class EnvironmentLauncher:
    """Owns every process/thread of the standalone environment."""

    def __init__(
        self,
        config: AssistantConfig,
        *,
        state: EnvironmentState | None = None,
        headless: bool = False,
        port: int = DEFAULT_PORT,
        open_dashboard: bool | None = None,
    ) -> None:
        self.config = config
        self.state = state or get_state()
        self.headless = bool(headless)
        self.port = int(port or DEFAULT_PORT)
        self.open_dashboard = (not headless) if open_dashboard is None else bool(open_dashboard)
        self.gate = PermissionGate(config=config, mode=config.default_mode)
        self.registry = ToolRegistry(config=config, gate=self.gate)
        self.adapter: Any = None
        self.loop: AgentLoop | None = None
        self.mouth: PiperMouth | None = None
        self.ears: WhisperEars | None = None
        self.voice: EnvironmentVoiceController | None = None
        self.server: EnvironmentServer | None = None
        self.telemetry: TelemetryCollector | None = None
        self.llama: LlamaServerManager | None = None
        self._dashboard_proc: subprocess.Popen[bytes] | None = None
        self._stop_event = threading.Event()
        self._started = False

    # -- setup -------------------------------------------------------------------

    def _build_registry(self) -> None:
        for fn in TOOL_FUNCTIONS:
            self.registry.register(fn)
        try:
            from assistant.brain.skills import configure_runtime

            configure_runtime(self.config)
        except Exception:
            pass

    def _ensure_brain(self) -> bool:
        if self.config.backend != "local":
            return True
        self.llama = LlamaServerManager(self.config)
        try:
            self.llama.ensure_running(progress=lambda line: logger.info("[llama-server] %s", line))
            return True
        except LlamaServerError as exc:
            logger.error("llama-server unavailable: %s", exc)
            self.state.set_thought(f"llama-server unavailable: {exc}")
            self.state.set_status("error")
            return False

    def _brain(self, text: str, source: str = "api") -> dict[str, Any]:
        """Fast actions + a genuine LLM sentence; open questions go through AgentLoop."""
        tools_called: list[str] = []
        started = time.perf_counter()
        command = match_fast_command(text)
        if command is not None:
            self.state.set_thought(f"Running {command.get('tool') or 'workflow'}…")
            reply = run_fast_command(self.registry, command)
            tool_name = command.get("tool")
            if tool_name:
                tools_called.append(str(tool_name))
            for sub in command.get("commands") or []:  # type: ignore[union-attr]
                sub_tool = dict(sub).get("tool")
                if sub_tool:
                    tools_called.append(str(sub_tool))
            natural = self._natural_reply(text, reply, tools_called)
            return {
                "reply": natural or reply,
                "tools_called": tools_called,
                "llm_ms": (time.perf_counter() - started) * 1000.0,
            }
        if self.loop is None:
            return {"reply": "", "tools_called": [], "llm_ms": 0.0}
        self.registry.on_execute = lambda name: tools_called.append(name)
        try:
            result = self.loop.run_task(text)
            reply = result.reply
        finally:
            self.registry.on_execute = None
        return {
            "reply": reply,
            "tools_called": list(dict.fromkeys(tools_called)),
            "llm_ms": (time.perf_counter() - started) * 1000.0,
        }

    def _natural_reply(self, user_text: str, tool_reply: str, tools: list[str]) -> str:
        """Ask the local model to phrase a real tool outcome naturally (brain_replies)."""
        if not self.state.get_config().get("brain_replies", True):
            return tool_reply
        if self.adapter is None or not tool_reply:
            return tool_reply
        live = self.state.get_config()
        prompt = (
            f"User asked: {user_text}\n"
            f"Real tool result: {tool_reply}\n"
            f"Tools used: {', '.join(tools) or 'none'}\n"
            "Reply with one natural spoken sentence that states the real outcome. "
            "Do not invent extra facts."
        )
        try:
            response, _usage = self.adapter.generate_turn(
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                system_instruction=build_chat_prompt(
                    self.config,
                    personality=str(live.get("personality_prompt") or ""),
                    user_name=str(live.get("user_name") or ""),
                    reply_style=str(live.get("reply_style") or "natural"),
                ),
                tools=None,
                max_output_tokens=120,
            )
            text = (response.text or "").strip()
            return text or tool_reply
        except Exception as exc:
            logger.info("Natural reply fallback to the tool sentence: %s", type(exc).__name__)
            return tool_reply

    def _apply_gate_config(self, live: dict[str, Any]) -> None:
        mode = str(live.get("permission_mode") or "")
        if mode:
            self.gate.mode = mode

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        if self._started:
            return {"url": self.server.url if self.server else "", "port": self.port}
        self.state.set_status("starting")
        self.state.set_thought("Starting the Ultron environment…")
        self._build_registry()
        self._ensure_brain()
        self.adapter = build_adapter(self.config)
        watchdog = WatchdogMonitor(
            self.config.max_steps, self.config.wall_clock_timeout_sec, self.config.idle_standdown_sec
        )
        self.loop = AgentLoop(self.config, self.adapter, self.registry, watchdog)

        if not os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("ULTRON_NO_INPUT"):
            try:
                start_ydotool()
            except Exception as exc:
                logger.warning("ydotoold unavailable: %s", type(exc).__name__)
        ensure_accessibility()
        start_screen_watcher(self.config.screen_watch_interval)

        self.mouth = PiperMouth(self.config)
        self.ears = WhisperEars(self.config)
        self.voice = EnvironmentVoiceController(
            self.config,
            state=self.state,
            ears=self.ears,
            mouth=self.mouth,
            brain=self._brain,
            vad=get_shared_vad(),
        )
        set_timer_speaker(lambda text: self.mouth.speak(text) if self.mouth else None)
        self.voice.start()

        self.telemetry = TelemetryCollector(self.state)
        self.telemetry.start()

        self.server = EnvironmentServer(
            self.config,
            state=self.state,
            port=self.port,
            brain=self._brain,
            voice=self.voice,
            bridge=get_browser_bridge(),
            on_config=self._apply_gate_config,
            speak=lambda text: self.mouth.speak(text) if self.mouth else None,
        )
        url = self.server.start()
        self.port = self.server.port
        if self.open_dashboard:
            self._open_dashboard(url)
        self.state.set_status("listening")
        self.state.set_thought("Ready. Say “Ultron” or use the Dashboard.")
        self._started = True
        logger.info("Ultron environment ready on %s (%s)", url, os_release_name())
        return {"url": url, "port": self.port, "llama": bool(self.llama), "voice": True}

    def _open_dashboard(self, url: str) -> None:
        commands: list[list[str]] = []
        if shutil.which("brave-browser"):
            commands.append(["brave-browser", f"--app={url}", "--force-renderer-accessibility"])
        if shutil.which("google-chrome"):
            commands.append(["google-chrome", f"--app={url}", "--force-renderer-accessibility"])
        if shutil.which("firefox"):
            commands.append(["firefox", "--new-window", url])
        if shutil.which("xdg-open"):
            commands.append(["xdg-open", url])
        for argv in commands:
            try:
                self._dashboard_proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
                logger.info("Dashboard window opened with %s", argv[0])
                return
            except OSError:
                continue
        logger.warning("No browser found for the Dashboard; open %s manually.", url)

    def wait(self) -> None:
        self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception:
                pass
            self.voice = None
        try:
            stop_screen_watcher()
        except Exception:
            pass
        if not os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("ULTRON_NO_INPUT"):
            try:
                stop_ydotool()
            except Exception:
                pass
        if self.telemetry is not None:
            self.telemetry.stop()
            if self.telemetry is not threading.current_thread():
                self.telemetry.join(timeout=2.0)
            self.telemetry = None
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None
        if self.llama is not None:
            try:
                self.llama.close()
            except Exception:
                pass
            self.llama = None
        if self._dashboard_proc is not None and self._dashboard_proc.poll() is None:
            try:
                self._dashboard_proc.terminate()
            except Exception:
                pass
            self._dashboard_proc = None
        self.state.set_status("online")
        self.state.set_thought("Environment stopped.")
        self._started = False
        logger.info("Ultron environment stopped.")


def run_environment(config: AssistantConfig, *, headless: bool = False, port: int | None = None) -> int:
    """CLI entry point used by ``main.py environment``."""
    launcher = EnvironmentLauncher(config, headless=headless, port=int(port or DEFAULT_PORT))

    def _signal(_signum: int, _frame: Any) -> None:
        launcher.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):
            pass
    try:
        info = launcher.start()
    except OSError as exc:
        print(f"[ERROR] Could not start the environment server on port {launcher.port}: {exc}")
        launcher.stop()
        return 1
    except Exception as exc:
        print(f"[ERROR] Environment failed to start: {type(exc).__name__}: {exc}")
        launcher.stop()
        return 1
    print(f"Ultron environment ready on {info['url']}  (Ctrl+C to stop)")
    print(f"  state     : {info['url']}/api/state")
    print(f"  live SSE  : {info['url']}/api/stream")
    print(f"  dashboard : {info['url']}/")
    if headless:
        print("  headless  : no browser window was opened")
    try:
        launcher.wait()
    except KeyboardInterrupt:
        pass
    finally:
        launcher.stop()
    return 0
