"""Ultron's local environment API: REST + SSE on 127.0.0.1:9932.

The Gate 2B Liquid Glass Dashboard talks only to this server. It is stdlib-only
(``http.server``), bound to localhost, CORS-enabled for local dashboards, and serves a
small readiness page until the real dashboard is built in ``assistant/dashboard/``.

No endpoint executes arbitrary shell commands; app/tab/skill actions go through the same
permission-aware tools as the voice/CLI paths.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from assistant.brain.skills import (
    delete_user_alias,
    delete_user_fact,
    delete_user_skill,
    load_skills,
    load_user_memory,
    save_user_alias,
    save_user_fact,
    save_user_skill,
    split_actions,
)
from assistant.config import AssistantConfig, PROJECT_ROOT, load_config
from assistant.environment.browser_bridge import BrowserBridgeHub, get_browser_bridge
from assistant.environment.state import EnvironmentState, get_state
from assistant.tools.apps import close_app, find_app, open_app, scan_desktop_apps, switch_app
from assistant.tools.browser_tabs import interact_with_tab, list_browser_tabs, switch_to_tab
from assistant.tools.desktop_control import press_key
from assistant.tools.screen_actions import click_element, focus_and_type, read_screen

logger = logging.getLogger("ultron.environment.server")

# The Dashboard contract pins the classic JS MIME type (some systems default to
# text/javascript, which older tooling rejects).
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

VERSION = "phase9-env"
DEFAULT_PORT = int(os.environ.get("ULTRON_ENV_PORT") or 9932)
MAX_BODY_BYTES = 1_048_576
DASHBOARD_CANDIDATES = (
    PROJECT_ROOT / "assistant" / "dashboard" / "dist",
    PROJECT_ROOT / "assistant" / "dashboard" / "static",
)

PLACEHOLDER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ultron Environment</title>
<style>
  :root { color-scheme: dark; --glass: rgba(255,255,255,.08); --line: rgba(255,255,255,.16); }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; font-family: ui-sans-serif, system-ui, sans-serif;
         color:#eaf0ff; background: radial-gradient(1200px 800px at 15% 0%, #24345c, #070b13 65%); }
  main { max-width: 960px; margin: 0 auto; padding: 56px 24px; }
  .glass { background: var(--glass); border: 1px solid var(--line); border-radius: 28px;
           padding: 28px; box-shadow: 0 20px 80px rgba(0,0,0,.35); backdrop-filter: blur(18px); }
  h1 { margin: 0 0 8px; font-weight: 600; letter-spacing: -0.03em; }
  p { color:#aab7d4; line-height:1.55; }
  .pill { display:inline-flex; gap:8px; align-items:center; padding:6px 12px; border-radius:999px;
          background: rgba(80,220,160,.14); color:#8ff0c6; font-size:13px; }
  .dot { width:8px; height:8px; border-radius:50%; background:#5ee6a8; box-shadow:0 0 12px #5ee6a8; }
  code { background:rgba(255,255,255,.09); padding:2px 7px; border-radius:7px; color:#cfe0ff; }
  ul { color:#aab7d4; line-height:1.9; }
</style></head><body><main>
  <div class="glass">
    <span class="pill"><span class="dot"></span> environment online</span>
    <h1>Ultron Environment API</h1>
    <p>The standalone engine is running on <code>http://127.0.0.1:9932</code>.
       The Apple visionOS Liquid Glass V18 dashboard will be served from this address once
       the Gate 2B UI agent places its build in <code>assistant/dashboard/dist/</code>.</p>
    <ul>
      <li><code>GET /api/health</code> — liveness</li>
      <li><code>GET /api/state</code> — full live state</li>
      <li><code>GET /api/stream</code> — SSE state snapshots</li>
      <li><code>GET /api/tabs</code> — open browser tabs</li>
      <li><code>GET /api/skills</code> — facts, aliases and workflows</li>
    </ul>
    <p>Contract: <code>docs/DASHBOARD_API_CONTRACT.md</code></p>
  </div>
</main></body></html>"""


class EnvironmentServer:
    """Owner of the ThreadingHTTPServer and all API handlers."""

    def __init__(
        self,
        config: AssistantConfig | None = None,
        *,
        state: EnvironmentState | None = None,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        brain: Callable[..., Any] | None = None,
        voice: Any | None = None,
        bridge: BrowserBridgeHub | None = None,
        dashboard_dir: Path | str | None = None,
        on_config: Callable[[dict[str, Any]], None] | None = None,
        speak: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config or load_config()
        self.state = state or get_state()
        self.host = host
        self.port = int(port)
        self.brain = brain
        self.voice = voice
        self.bridge = bridge or get_browser_bridge()
        self.dashboard_dir = Path(dashboard_dir) if dashboard_dir else self._find_dashboard_dir()
        self.on_config = on_config
        self.speak = speak
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------

    @staticmethod
    def _find_dashboard_dir() -> Path | None:
        for candidate in DASHBOARD_CANDIDATES:
            if (candidate / "index.html").is_file():
                return candidate
        return None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        httpd = ThreadingHTTPServer((self.host, self.port), _RequestHandler)
        httpd.daemon_threads = True
        httpd.app = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.15},
            name="ultron-environment-http",
            daemon=True,
        )
        self._thread.start()
        logger.info("Environment server listening on %s", self.url)
        return self.url

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        logger.info("Environment server stopped.")

    @property
    def running(self) -> bool:
        return self._httpd is not None

    # -- routing -----------------------------------------------------------------

    def dispatch(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> tuple[int, dict[str, Any] | str | bytes, str]:
        """Return (status, payload, content_type). Payload dict is sent as JSON."""
        if path == "/api/health":
            return 200, self._health(), "application/json"
        if path == "/api/state":
            return 200, self.state.snapshot(), "application/json"
        if path == "/api/config":
            if method == "GET":
                return 200, {"ok": True, "config": self.state.get_config()}, "application/json"
            if method == "POST":
                return self._update_config(body)
            return 405, {"ok": False, "error": "method not allowed"}, "application/json"
        if path == "/api/command" and method == "POST":
            return self._command(body)
        if path == "/api/voice/control" and method == "POST":
            return self._voice_control(body)
        if path == "/api/apps":
            if method == "GET":
                return 200, self._apps(), "application/json"
            return 405, {"ok": False, "error": "method not allowed"}, "application/json"
        if path == "/api/apps/action" and method == "POST":
            return self._apps_action(body)
        if path == "/api/tabs":
            if method == "GET":
                return 200, self._tabs(), "application/json"
            return 405, {"ok": False, "error": "method not allowed"}, "application/json"
        if path == "/api/tabs/action" and method == "POST":
            return self._tabs_action(body)
        if path == "/api/screen/action" and method == "POST":
            return self._screen_action(body)
        if path == "/api/browser/sync" and method == "POST":
            return 200, self.bridge.sync(body), "application/json"
        if path == "/api/browser/poll" and method == "GET":
            browser = (query.get("browser") or [""])[0]
            return 200, {"ok": True, "actions": self.bridge.poll(browser)}, "application/json"
        if path == "/api/browser/result" and method == "POST":
            self.bridge.record_result(body)
            return 200, {"ok": True}, "application/json"
        if path == "/api/skills":
            if method == "GET":
                return 200, self._skills(), "application/json"
            if method == "POST":
                return self._skills_post(body)
            if method == "DELETE":
                return self._skills_delete(body, query)
            return 405, {"ok": False, "error": "method not allowed"}, "application/json"
        if path == "/favicon.ico":
            return 204, b"", "image/x-icon"
        return 404, {"ok": False, "error": f"no route for {path}"}, "application/json"

    # -- handlers ----------------------------------------------------------------

    def _health(self) -> dict[str, Any]:
        llama = False
        try:
            from assistant.brain.server_manager import port_is_open

            llama = bool(port_is_open("127.0.0.1", 9931))
        except Exception:
            llama = False
        return {
            "ok": True,
            "version": VERSION,
            "llama_server": llama,
            "voice_status": self.state.snapshot()["status"],
        }

    def _command(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        text = str(body.get("text") or "").strip()
        if not text:
            return 400, {"ok": False, "error": "text is required"}, "application/json"
        if self.brain is None:
            return 503, {"ok": False, "error": "brain is not attached to the environment server"}, "application/json"
        speak = bool(body.get("speak", True))
        self.state.set_transcript(text)
        self.state.add_turn("user", text, source="api")
        self.state.set_thought(f"Thinking about: {text}")
        self.state.set_status("thinking")
        started = time.perf_counter()
        try:
            try:
                result = self.brain(text, source="api")
            except TypeError:
                result = self.brain(text)
        except Exception as exc:
            self.state.set_status("error")
            self.state.set_thought(f"Brain error: {type(exc).__name__}")
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        elapsed = (time.perf_counter() - started) * 1000.0
        if isinstance(result, str):
            reply, tools, llm_ms, tool_ms = result, [], elapsed, 0.0
        else:
            reply = str((result or {}).get("reply") or "")
            tools = [str(item) for item in ((result or {}).get("tools_called") or [])]
            llm_ms = float((result or {}).get("llm_ms") or (result or {}).get("latency_ms") or elapsed)
            tool_ms = float((result or {}).get("tool_ms") or 0.0)
        self.state.add_latency(llm_ms=llm_ms, tool_ms=tool_ms, total_ms=elapsed)
        if reply:
            self.state.add_turn(
                "assistant", reply, tools_called=tools, latency_ms=elapsed, source="api"
            )
            self.state.set_reply(reply)
            if speak:
                self._speak(reply)
        self.state.set_thought("")
        self.state.set_action("", {}, "idle")
        self.state.set_status("online")
        return 200, {
            "ok": True,
            "reply": reply,
            "tools_called": tools,
            "latency_ms": round(elapsed, 2),
        }, "application/json"

    def _speak(self, text: str) -> None:
        mouth = getattr(self.voice, "mouth", None)
        try:
            if self.speak is not None:
                self.speak(text)
            elif mouth is not None and hasattr(mouth, "speak"):
                mouth.speak(text)
        except Exception as exc:
            logger.warning("Speak failed: %s", type(exc).__name__)

    def _voice_control(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        action = str(body.get("action") or "").strip().lower()
        mode = str(body.get("mode") or "").strip().lower()
        if mode:
            if not self.state.set_voice_mode(mode):
                return 400, {"ok": False, "error": f"unknown voice mode {mode!r}"}, "application/json"
            if self.voice is not None and hasattr(self.voice, "set_mode"):
                self.voice.set_mode(mode)
        if self.voice is None:
            if action in ("", "mode"):
                return 200, {"ok": True, "voice_mode": self.state.snapshot()["voice_mode"]}, "application/json"
            return 503, {"ok": False, "error": "voice controller is not running"}, "application/json"
        handlers = {
            "start": self.voice.start,
            "stop": self.voice.stop,
            "mute_mic": lambda: self.voice.mute_mic(True),
            "unmute_mic": lambda: self.voice.mute_mic(False),
            "mute_tts": lambda: self.voice.mute_tts(True),
            "unmute_tts": lambda: self.voice.mute_tts(False),
            "barge_in": self.voice.barge_in,
            "ptt_start": self.voice.start_ptt,
            "ptt_stop": self.voice.stop_ptt,
        }
        if action and action not in handlers:
            return 400, {"ok": False, "error": f"unknown voice action {action!r}"}, "application/json"
        try:
            result = handlers[action]() if action else None
        except Exception as exc:
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        return 200, {
            "ok": True,
            "action": action,
            "result": result,
            "voice_mode": self.state.snapshot()["voice_mode"],
            "status": self.state.snapshot()["status"],
        }, "application/json"

    def _update_config(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        patch = body.get("config") if isinstance(body.get("config"), dict) else body
        if not isinstance(patch, dict) or not patch:
            return 400, {"ok": False, "error": "a config object is required"}, "application/json"
        updated = self.state.update_config(patch)
        self._apply_config(updated)
        return 200, {"ok": True, "config": updated}, "application/json"

    def _apply_config(self, live: dict[str, Any]) -> None:
        cfg = self.config
        for attr in (
            "assistant_name",
            "voice_silence_ms",
            "voice_max_seconds",
            "listen_early_check_ms",
            "listen_followup_s",
            "mic_source_volume",
            "search_engine",
        ):
            if attr in live:
                setattr(cfg, attr, live[attr])
        if "permission_mode" in live:
            cfg.default_mode = live["permission_mode"]
        try:
            if self.voice is not None and hasattr(self.voice, "apply_config"):
                self.voice.apply_config()
        except Exception:
            pass
        if self.on_config is not None:
            try:
                self.on_config(live)
            except Exception as exc:
                logger.warning("Config callback failed: %s", type(exc).__name__)

    def _apps(self) -> dict[str, Any]:
        from assistant.brain.context import SESSION

        running_names = _running_process_names()
        rows: dict[str, dict[str, Any]] = {}
        for app in scan_desktop_apps():
            key = app.key or app.name.lower()
            if key in rows or not app.name:
                continue
            rows[key] = {
                "id": key,
                "name": app.name,
                "desktop_id": app.desktop_id,
                "desktop_path": app.desktop_path,
                "category": app.source or "system",
                "is_running": _is_running(app, running_names),
            }
        # YAML-only entries (Lutris, Hermes, GNOME Settings…) win over a scan duplicate.
        try:
            from assistant.tools import apps as apps_module

            for key, entry in (apps_module._catalog() or {}).items():  # noqa: SLF001
                if not isinstance(entry, dict):
                    continue
                match = find_app(key)
                if match is None:
                    continue
                rows[key] = {
                    "id": key,
                    "name": match.display,
                    "desktop_id": match.desktop_id,
                    "desktop_path": match.desktop_path,
                    "category": "catalog",
                    "is_running": any(name in running_names for name in match.process_names),
                }
        except Exception:
            pass
        return {
            "ok": True,
            "apps": sorted(rows.values(), key=lambda row: row["name"].lower()),
            "windows": list(SESSION.open_windows),
            "focused_window": SESSION.focused_window,
        }

    def _apps_action(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        action = str(body.get("action") or "").strip().lower()
        app_name = str(body.get("app_name") or body.get("name") or "").strip()
        if not app_name:
            return 400, {"ok": False, "error": "app_name is required"}, "application/json"
        self.state.set_action(f"apps_{action}", {"app_name": app_name}, "running")
        try:
            if action == "open":
                result = open_app(app_name)
            elif action == "close":
                result = close_app(app_name)
            elif action == "focus":
                result = switch_app(app_name)
            else:
                return 400, {"ok": False, "error": f"unknown app action {action!r}"}, "application/json"
        except Exception as exc:
            self.state.set_action("", {}, "idle")
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        self.state.set_action(f"apps_{action}", {"app_name": app_name}, "done")
        return 200, result, "application/json"

    def _tabs(self) -> dict[str, Any]:
        tabs = self.bridge.list_tabs()
        return {
            "ok": True,
            "count": len(tabs),
            "tabs": tabs,
            "bridge_connected": self.bridge.connected,
        }

    def _tabs_action(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        action = str(body.get("action") or "").strip().lower()
        query = str(body.get("tab") or body.get("query") or body.get("tab_query") or "").strip()
        target = str(body.get("target") or "")
        text = str(body.get("text") or "")
        self.state.set_action(f"tabs_{action}", {"query": query}, "running")
        try:
            if action == "switch":
                result = self.bridge.switch_tab(query)
                if not result.get("ok"):
                    result = switch_to_tab(query)
            elif action == "close":
                tab = self.bridge.find_tab(query)
                if tab is None:
                    result = {"ok": False, "error": f"I couldn't find a tab matching {query!r}."}
                else:
                    queued = self.bridge.queue_action("close_tab", tab=tab)
                    result = {"ok": True, "tab": tab, "action_id": queued["id"]}
            elif action == "open":
                url = str(body.get("url") or query)
                queued = self.bridge.queue_action("open_url", text=url)
                result = {"ok": True, "action_id": queued["id"], "url": url}
            elif action in ("click", "read", "type", "send", "scroll"):
                result = self.bridge.interact(query, action, target=target, text=text)
                if not result.get("ok") and not self.bridge.connected:
                    result = interact_with_tab(query, action, target=target, text=text)
            else:
                return 400, {"ok": False, "error": f"unknown tab action {action!r}"}, "application/json"
        except Exception as exc:
            self.state.set_action("", {}, "idle")
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        self.state.set_action(f"tabs_{action}", {"query": query}, "done")
        return 200, result, "application/json"

    def _screen_action(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        """Click/read/type a live AT-SPI ``@e`` element on the current screen."""
        action = str(body.get("action") or "").strip().lower()
        ref = str(body.get("ref") or body.get("target") or "").strip()
        text = str(body.get("text") or "").strip()
        self.state.set_action(f"screen_{action}", {"ref": ref}, "running")
        try:
            if action == "click":
                result = click_element(ref)
            elif action == "read":
                result = read_screen(ref)
            elif action in ("type", "send"):
                result = focus_and_type(ref or "message box", text)
                if action == "send" and result.get("ok"):
                    press_key("enter")
                    result = {**result, "submitted": True}
            else:
                return 400, {"ok": False, "error": f"unknown screen action {action!r}"}, "application/json"
        except Exception as exc:
            self.state.set_action("", {}, "idle")
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        self.state.set_action(f"screen_{action}", {"ref": ref}, "done")
        return 200, result, "application/json"

    def _skills(self) -> dict[str, Any]:
        memory = load_user_memory(self.config)
        skills = [
            {"trigger": skill.trigger, "actions": list(skill.actions), "source": skill.source}
            for skill in load_skills(self.config)
        ]
        return {
            "ok": True,
            "facts": memory["facts"],
            "aliases": memory["aliases"],
            "skills": skills,
        }

    def _skills_post(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        kind = str(body.get("type") or "fact").strip().lower()
        try:
            if kind == "fact":
                key = save_user_fact(self.config, str(body.get("name") or body.get("key") or ""), str(body.get("value") or ""))
                saved = {"type": "fact", "name": key}
            elif kind == "alias":
                alias, target = save_user_alias(self.config, str(body.get("alias") or ""), str(body.get("target") or ""))
                saved = {"type": "alias", "alias": alias, "target": target}
            elif kind == "skill":
                actions = body.get("actions")
                if isinstance(actions, str):
                    actions = split_actions(actions)
                elif not isinstance(actions, (list, tuple)):
                    actions = split_actions(str(body.get("actions_text") or ""))
                skill = save_user_skill(self.config, str(body.get("trigger") or ""), actions)
                saved = {"type": "skill", "trigger": skill.trigger, "actions": list(skill.actions)}
            else:
                return 400, {"ok": False, "error": f"unknown skill type {kind!r}"}, "application/json"
        except (OSError, ValueError) as exc:
            return 400, {"ok": False, "error": str(exc)}, "application/json"
        return 200, {"ok": True, "saved": saved, **self._skills()}, "application/json"

    def _skills_delete(
        self, body: dict[str, Any], query: dict[str, list[str]]
    ) -> tuple[int, dict[str, Any], str]:
        kind = str(body.get("type") or (query.get("type") or ["fact"])[0]).strip().lower()
        try:
            if kind == "fact":
                name = str(body.get("name") or body.get("key") or (query.get("name") or [""])[0])
                removed = delete_user_fact(self.config, name)
            elif kind == "alias":
                alias = str(body.get("alias") or (query.get("alias") or [""])[0])
                removed = delete_user_alias(self.config, alias)
            elif kind == "skill":
                trigger = str(body.get("trigger") or (query.get("trigger") or [""])[0])
                removed = delete_user_skill(self.config, trigger)
            else:
                return 400, {"ok": False, "error": f"unknown skill type {kind!r}"}, "application/json"
        except (OSError, ValueError) as exc:
            return 400, {"ok": False, "error": str(exc)}, "application/json"
        return 200, {"ok": True, "removed": removed, **self._skills()}, "application/json"

    # -- static dashboard --------------------------------------------------------

    def static_response(self, path: str) -> tuple[int, bytes, str]:
        index = (self.dashboard_dir / "index.html") if self.dashboard_dir else None
        if index is None or not index.is_file():
            return 200, PLACEHOLDER_HTML.encode("utf-8"), "text/html; charset=utf-8"
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (self.dashboard_dir / relative).resolve()
        try:
            target.relative_to(self.dashboard_dir.resolve())
        except ValueError:
            return 404, b"not found", "text/plain; charset=utf-8"
        if not target.is_file():
            target = index
        try:
            data = target.read_bytes()
        except OSError:
            return 404, b"not found", "text/plain; charset=utf-8"
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix in (".html", ".css", ".js", ".json", ".svg"):
            content_type += "; charset=utf-8"
        return 200, data, content_type


# --------------------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------------------


def _running_process_names() -> set[str]:
    names: set[str] = set()
    try:
        import psutil

        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").strip().lower()
            if name:
                names.add(name)
    except Exception:
        return names
    return names


def _is_running(app: Any, running_names: set[str]) -> bool:
    candidates = [Path(str(getattr(app, "exec_argv", [""])[0] if getattr(app, "exec_argv", ()) else "")).name]
    if getattr(app, "try_exec", ""):
        candidates.append(Path(app.try_exec).name)
    for name in candidates:
        if not name:
            continue
        lowered = name.lower()
        if lowered in running_names or lowered[:15] in running_names:
            return True
    return False


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UltronEnvironment/phase9"

    @property
    def app(self) -> EnvironmentServer:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # keep test output quiet
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- CORS / responses --------------------------------------------------------

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _send(self, status: int, payload: dict[str, Any] | str | bytes, content_type: str) -> None:
        if isinstance(payload, dict):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- verbs -------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/stream":
            self._stream()
            return
        if not parsed.path.startswith("/api/"):
            status, payload, content_type = self.app.static_response(parsed.path)
            self._send(status, payload, content_type)
            return
        try:
            status, payload, content_type = self.app.dispatch(
                "GET", parsed.path, parse_qs(parsed.query), {}
            )
        except Exception as exc:
            status, payload, content_type = 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        self._send(status, payload, content_type)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_body()
        try:
            status, payload, content_type = self.app.dispatch(
                "POST", parsed.path, parse_qs(parsed.query), body
            )
        except Exception as exc:
            status, payload, content_type = 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        self._send(status, payload, content_type)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_body()
        try:
            status, payload, content_type = self.app.dispatch(
                "DELETE", parsed.path, parse_qs(parsed.query), body
            )
        except Exception as exc:
            status, payload, content_type = 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, "application/json"
        self._send(status, payload, content_type)

    def _stream(self) -> None:
        """Server-Sent Events: a full snapshot immediately, then on every change / 250 ms."""
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        last = -1
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            while True:
                snapshot = self.app.state.snapshot()
                revision = int(snapshot.get("revision") or 0)
                data = json.dumps(snapshot, ensure_ascii=False, default=str)
                self.wfile.write(
                    f"id: {revision}\nevent: state\ndata: {data}\n\n".encode("utf-8")
                )
                self.wfile.flush()
                last = revision
                self.app.state.wait_for_change(last, timeout=0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def run_environment_server(server: EnvironmentServer) -> str:
    """Convenience wrapper used by the launcher."""
    return server.start()
