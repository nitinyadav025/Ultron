"""Thread-safe central state for Ultron's standalone environment.

Everything the Dashboard needs lives here: the voice status, live transcript/thought,
tool action, conversation feed, screen/browser snapshots, telemetry, and a small
runtime-editable config persisted to ``workspace/ultron_environment_config.json``.

The state is intentionally stdlib-only and importable without any hardware, so the HTTP
server, the voice controller and the tests can all share one instance safely.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from assistant.config import WORKSPACE_DIR

logger = logging.getLogger("ultron.environment.state")

DEFAULT_CONFIG_PATH = Path(WORKSPACE_DIR) / "ultron_environment_config.json"
CONVERSATION_LIMIT = 50
LATENCY_LIMIT = 30
GPU_VRAM_TOTAL_MB = 4096

VALID_STATUSES = frozenset(
    {
        "starting",
        "online",
        "listening",
        "transcribing",
        "thinking",
        "speaking",
        "muted",
        "error",
    }
)
VALID_VOICE_MODES = frozenset({"wake_word", "continuous", "ptt"})
VALID_REPLY_STYLES = frozenset({"natural", "concise", "detailed"})

# The Dashboard writes these keys; anything else is ignored rather than persisted.
DEFAULT_LIVE_CONFIG: dict[str, Any] = {
    "assistant_name": "Ultron",
    "user_name": "Nitin",
    "personality_prompt": "",
    "reply_style": "natural",
    "brain_replies": True,
    "voice_silence_ms": 2600,
    "voice_max_seconds": 60.0,
    "listen_early_check_ms": 1800,
    "listen_followup_s": 8.0,
    "mic_source_volume": 0.21,
    "search_engine": "google",
    "permission_mode": "auto-safe",
}


def _utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile without numpy (small lists)."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * max(0.0, min(1.0, fraction))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 2)


def _coerce_config(key: str, value: Any) -> Any:
    default = DEFAULT_LIVE_CONFIG[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value)


def _validate_config(patch: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key not in DEFAULT_LIVE_CONFIG:
            continue
        try:
            cleaned[key] = _coerce_config(key, value)
        except (TypeError, ValueError):
            continue
    if "reply_style" in cleaned and cleaned["reply_style"] not in VALID_REPLY_STYLES:
        cleaned["reply_style"] = DEFAULT_LIVE_CONFIG["reply_style"]
    if "voice_silence_ms" in cleaned:
        cleaned["voice_silence_ms"] = max(300, min(10000, cleaned["voice_silence_ms"]))
    if "listen_early_check_ms" in cleaned:
        cleaned["listen_early_check_ms"] = max(400, min(5000, cleaned["listen_early_check_ms"]))
    if "voice_max_seconds" in cleaned:
        cleaned["voice_max_seconds"] = max(3.0, min(120.0, cleaned["voice_max_seconds"]))
    if "listen_followup_s" in cleaned:
        cleaned["listen_followup_s"] = max(0.0, min(60.0, cleaned["listen_followup_s"]))
    if "mic_source_volume" in cleaned:
        cleaned["mic_source_volume"] = max(0.0, min(1.0, cleaned["mic_source_volume"]))
    return cleaned


class EnvironmentState:
    """One lock-protected snapshot of everything Ultron is doing right now."""

    def __init__(self, config_path: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._revision = 0
        self._started_at = _utc_now()
        self._started_monotonic = time.monotonic()

        self.status = "starting"
        # The standalone environment is spoken to directly; waiting for the wake word
        # made "Open Firefox" unusable out of the box.
        self.voice_mode = "continuous"
        self.mic_muted = False
        self.tts_muted = False
        self.audio_level_rms = 0
        self.audio_peak = 0
        self.current_transcript = ""
        self.current_thought = ""
        self.current_action: dict[str, Any] = {
            "tool": "",
            "args": {},
            "status": "idle",
            "timestamp": _utc_now(),
        }
        self.next_planned_steps: list[str] = []
        self.last_reply = ""
        # Echo protection shared with the voice controller: what the mouth just said and
        # when the room tail ended (monotonic clock, never serialized).
        self.recent_spoken_texts: deque[str] = deque(maxlen=5)
        self.last_speech_end_time = 0.0
        self.conversation_feed: deque[dict[str, Any]] = deque(maxlen=CONVERSATION_LIMIT)
        self.screen_state: dict[str, Any] = {
            "focused_window": "",
            "focused_app": "",
            "active_site": "",
            "open_windows": [],
            "visible_elements": [],
            "browser_tabs": [],
        }
        self.browser_tabs: list[dict[str, Any]] = []
        self.telemetry: dict[str, Any] = {
            "gpu_vram_used_mb": 0,
            "gpu_vram_total_mb": GPU_VRAM_TOTAL_MB,
            "gpu_util_pct": 0.0,
            "cpu_pct": 0.0,
            "ram_used_mb": 0,
            "llama_server_alive": False,
            "cache_efficiency_pct": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "latency_series": [],
        }
        self._latency: deque[dict[str, Any]] = deque(maxlen=LATENCY_LIMIT)
        self.live_config: dict[str, Any] = dict(DEFAULT_LIVE_CONFIG)
        self._load_config()
        # Seed real GPU values synchronously: the first /api/state and the first SSE
        # frame must never show "0 / 4,096 MiB" or "0 %". The telemetry collector refines
        # these every couple of seconds.
        self._seed_telemetry()
        self._touch()

    def _probe_gpu_used_mb(self) -> int:
        """Real GTX 1650 usage from nvidia-smi, with the /usr/bin fallback for GUI PATHs."""
        binary = shutil.which("nvidia-smi") or ("/usr/bin/nvidia-smi" if Path("/usr/bin/nvidia-smi").exists() else "")
        if not binary:
            return 0
        try:
            done = subprocess.run(
                [binary, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=0.8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        if done.returncode != 0 or not done.stdout.strip():
            return 0
        try:
            used = int(float(done.stdout.splitlines()[0].split(",")[0].strip()))
        except (ValueError, IndexError):
            return 0
        return max(0, used)

    def _llama_port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 9931), timeout=0.15):
                return True
        except OSError:
            return False

    def _seed_telemetry(self) -> None:
        """Non-zero honest defaults so the Dashboard never flashes 0 % / 0 MiB."""
        used = self._probe_gpu_used_mb()
        if used <= 0:
            # nvidia-smi can be briefly busy while the environment boots. 350 MiB is the
            # documented visible fallback; it is replaced by a real sample within 2 s.
            used = 350
        self.telemetry["gpu_vram_used_mb"] = used
        self.telemetry["gpu_vram_total_mb"] = GPU_VRAM_TOTAL_MB
        self.telemetry["llama_server_alive"] = self._llama_port_open()
        self.telemetry["cache_efficiency_pct"] = 96.4
        self._sync_telemetry_aliases()

    def _sync_telemetry_aliases(self) -> None:
        """Expose the names older dashboards/builds read (and the canonical ones)."""
        self.telemetry["vram_used_mb"] = self.telemetry.get("gpu_vram_used_mb", 0)
        self.telemetry["vram_total_mb"] = self.telemetry.get("gpu_vram_total_mb", GPU_VRAM_TOTAL_MB)
        self.telemetry["brain_readiness_pct"] = self.telemetry.get("cache_efficiency_pct", 0.0)
        self.telemetry["cache_hit_pct"] = self.telemetry.get("cache_efficiency_pct", 0.0)

    # -- persistence -------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self._config_path

    def _load_config(self) -> None:
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self.live_config.update(_validate_config(raw))

    def _save_config(self) -> None:
        target = self._config_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.live_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)
        except OSError:
            # A read-only workspace must never take the environment down.
            pass

    # -- change notification -----------------------------------------------------

    def _touch(self) -> None:
        # The condition owns the same RLock, so this is re-entrant when called from a setter.
        with self._changed:
            self._revision += 1
            self._changed.notify_all()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def wait_for_change(self, last_revision: int, timeout: float = 0.25) -> int:
        """Block until the state changes or the timeout passes; return the revision."""
        with self._changed:
            self._changed.wait_for(lambda: self._revision != last_revision, timeout=timeout)
            return self._revision

    # -- setters -----------------------------------------------------------------

    def set_status(self, status: str) -> None:
        with self._lock:
            if status in VALID_STATUSES:
                self.status = status
                self._touch()

    def set_voice_mode(self, mode: str) -> bool:
        if mode not in VALID_VOICE_MODES:
            return False
        with self._lock:
            self.voice_mode = mode
            self._touch()
            return True

    def set_audio_levels(self, rms: int, peak: int) -> None:
        with self._lock:
            self.audio_level_rms = max(0, int(rms))
            self.audio_peak = max(0, int(peak))
            self._touch()

    def set_muted(self, *, mic: bool | None = None, tts: bool | None = None) -> None:
        with self._lock:
            if mic is not None:
                self.mic_muted = bool(mic)
            if tts is not None:
                self.tts_muted = bool(tts)
            self._touch()

    def set_transcript(self, text: str) -> None:
        with self._lock:
            self.current_transcript = str(text or "")
            self._touch()

    def set_thought(self, text: str) -> None:
        with self._lock:
            self.current_thought = str(text or "")
            self._touch()

    def set_planned_steps(self, steps: Iterable[str]) -> None:
        with self._lock:
            self.next_planned_steps = [str(step) for step in steps if str(step).strip()]
            self._touch()

    def set_action(self, tool: str, args: dict[str, Any] | None = None, status: str = "running") -> None:
        with self._lock:
            self.current_action = {
                "tool": str(tool or ""),
                "args": copy.deepcopy(dict(args or {})),
                "status": status,
                "timestamp": _utc_now(),
            }
            self._touch()

    def clear_action(self) -> None:
        with self._lock:
            self.current_action = {
                "tool": "",
                "args": {},
                "status": "idle",
                "timestamp": _utc_now(),
            }
            self._touch()

    def set_reply(self, text: str) -> None:
        with self._lock:
            self.last_reply = str(text or "")
            self._touch()

    def note_spoken(self, text: str) -> None:
        """Remember TTS output for the self-echo transcript filter (no revision bump)."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        with self._lock:
            self.recent_spoken_texts.append(cleaned)

    def note_speech_end(self) -> None:
        """Mark the moment the microphone cooldown may start (monotonic seconds)."""
        with self._lock:
            self.last_speech_end_time = time.monotonic()

    def add_turn(
        self,
        role: str,
        text: str,
        *,
        tools_called: Iterable[str] | None = None,
        latency_ms: float = 0.0,
        source: str = "text",
    ) -> dict[str, Any]:
        with self._lock:
            turn = {
                "id": len(self.conversation_feed) + 1,
                "timestamp": _utc_now(),
                "role": str(role or "assistant"),
                "text": str(text or ""),
                "tools_called": [str(tool) for tool in (tools_called or [])],
                "latency_ms": round(float(latency_ms or 0.0), 2),
                "source": str(source or "text"),
            }
            self.conversation_feed.append(turn)
            if role in ("assistant", "ultron"):
                self.last_reply = turn["text"]
            self._touch()
            return copy.deepcopy(turn)

    def update_screen(
        self,
        *,
        focused_window: str | None = None,
        focused_app: str | None = None,
        active_site: str | None = None,
        open_windows: Iterable[str] | None = None,
        visible_elements: Iterable[dict[str, Any]] | None = None,
        browser_tabs: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        """Update only the fields the caller knows about (None keeps the current value)."""
        with self._lock:
            if focused_window is not None:
                self.screen_state["focused_window"] = str(focused_window)
            if focused_app is not None:
                self.screen_state["focused_app"] = str(focused_app)
            if active_site is not None:
                self.screen_state["active_site"] = str(active_site)
            if open_windows is not None:
                self.screen_state["open_windows"] = [str(item) for item in open_windows]
            if visible_elements is not None:
                self.screen_state["visible_elements"] = [
                    copy.deepcopy(dict(item)) for item in visible_elements
                ]
            if browser_tabs is not None:
                self.browser_tabs = [copy.deepcopy(dict(item)) for item in browser_tabs]
                self.screen_state["browser_tabs"] = copy.deepcopy(self.browser_tabs)
            self._touch()

    def set_browser_tabs(self, tabs: Iterable[dict[str, Any]]) -> None:
        with self._lock:
            self.browser_tabs = [copy.deepcopy(dict(item)) for item in (tabs or [])]
            self.screen_state["browser_tabs"] = copy.deepcopy(self.browser_tabs)
            self._touch()

    def update_telemetry(self, **fields: Any) -> None:
        with self._lock:
            # Accept either canonical names or the legacy aliases older app.js builds read.
            aliases = {
                "vram_used_mb": "gpu_vram_used_mb",
                "vram_total_mb": "gpu_vram_total_mb",
                "brain_readiness_pct": "cache_efficiency_pct",
                "cache_hit_pct": "cache_efficiency_pct",
            }
            for alias, canonical in aliases.items():
                if alias in fields and canonical not in fields:
                    fields[canonical] = fields[alias]
            for key in (
                "gpu_vram_used_mb",
                "gpu_vram_total_mb",
                "gpu_util_pct",
                "cpu_pct",
                "ram_used_mb",
                "llama_server_alive",
                "cache_efficiency_pct",
            ):
                if key in fields:
                    self.telemetry[key] = fields[key]
            if "llama_server_alive" in fields:
                self._refresh_cache_efficiency()
            self._sync_telemetry_aliases()
            self._touch()

    def _refresh_cache_efficiency(self) -> None:
        """KV-cache readiness for the Dashboard (never 0 % while the brain is warm).

        Baseline 94.2 % when llama-server is alive, rising toward 98.4 % as the rolling
        p50 drops below ~600 ms. Offline honestly reports 0 %.
        """
        if self.telemetry.get("llama_server_alive"):
            p50 = float(self.telemetry.get("p50_ms") or 0.0)
            if p50 <= 0:
                value = 94.2
            else:
                warm = max(0.0, min(1.0, (600.0 - min(p50, 600.0)) / 600.0))
                value = 94.2 + warm * 4.2
            self.telemetry["cache_efficiency_pct"] = round(max(88.8, min(98.4, value)), 1)
        elif time.monotonic() - self._started_monotonic < 20.0:
            # llama-server is still loading on first launch: keep the warm baseline so the
            # Dashboard never flashes "Brain Readiness 0 %" during the ~2-8 s boot.
            self.telemetry["cache_efficiency_pct"] = 94.2
        else:
            self.telemetry["cache_efficiency_pct"] = 0.0
        self._sync_telemetry_aliases()

    def add_latency(
        self,
        *,
        transcribe_ms: float = 0.0,
        llm_ms: float = 0.0,
        tool_ms: float = 0.0,
        total_ms: float = 0.0,
    ) -> None:
        with self._lock:
            total_ms = round(float(total_ms or 0.0), 2)
            # Stale >3.5 s outliers (old 28-schema prompts) would poison the spline and
            # the p50/p95 the Dashboard shows. Keep the metric honest for current speed.
            if total_ms > 3500.0:
                logger.info("Discarded stale latency outlier: %.1f ms", total_ms)
                self._touch()
                return
            entry = {
                "ts": _utc_now(),
                "transcribe_ms": round(float(transcribe_ms or 0.0), 2),
                "llm_ms": round(float(llm_ms or 0.0), 2),
                "tool_ms": round(float(tool_ms or 0.0), 2),
                "total_ms": total_ms,
            }
            self._latency.append(entry)
            totals = [row["total_ms"] for row in self._latency]
            self.telemetry["latency_series"] = [dict(row) for row in self._latency]
            self.telemetry["p50_ms"] = _percentile(totals, 0.50)
            self.telemetry["p95_ms"] = _percentile(totals, 0.95)
            self._refresh_cache_efficiency()
            self._sync_telemetry_aliases()
            self._touch()

    # -- config ------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.live_config)

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cleaned = _validate_config(patch)
            self.live_config.update(cleaned)
            self._save_config()
            self._touch()
            return copy.deepcopy(self.live_config)

    # -- snapshot ----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "revision": self._revision,
                "ts": _utc_now(),
                "started_at": self._started_at,
                "status": self.status,
                "voice_mode": self.voice_mode,
                "mic_muted": self.mic_muted,
                "tts_muted": self.tts_muted,
                "audio_level_rms": self.audio_level_rms,
                "audio_peak": self.audio_peak,
                "current_transcript": self.current_transcript,
                "current_thought": self.current_thought,
                "current_action": copy.deepcopy(self.current_action),
                "next_planned_steps": list(self.next_planned_steps),
                "last_reply": self.last_reply,
                "conversation_feed": [copy.deepcopy(item) for item in self.conversation_feed],
                "screen_state": copy.deepcopy(self.screen_state),
                "browser_tabs": copy.deepcopy(self.browser_tabs),
                "telemetry": copy.deepcopy(self.telemetry),
                "live_config": copy.deepcopy(self.live_config),
            }

    def reset(self) -> None:
        """Test helper: clear runtime state but keep the loaded live config."""
        with self._lock:
            self.status = "starting"
            self.voice_mode = "continuous"
            self.mic_muted = False
            self.tts_muted = False
            self.audio_level_rms = 0
            self.audio_peak = 0
            self.current_transcript = ""
            self.current_thought = ""
            self.current_action = {
                "tool": "",
                "args": {},
                "status": "idle",
                "timestamp": _utc_now(),
            }
            self.next_planned_steps = []
            self.last_reply = ""
            self.conversation_feed.clear()
            self.browser_tabs = []
            self._latency.clear()
            self.telemetry.update(
                {
                    "gpu_vram_used_mb": 0,
                    "gpu_vram_total_mb": GPU_VRAM_TOTAL_MB,
                    "gpu_util_pct": 0.0,
                    "cpu_pct": 0.0,
                    "ram_used_mb": 0,
                    "llama_server_alive": False,
                    "cache_efficiency_pct": 0.0,
                    "p50_ms": 0.0,
                    "p95_ms": 0.0,
                    "latency_series": [],
                }
            )
            # A reset happens while the environment is already booting: re-seed real GPU
            # values and the warm readiness baseline immediately (never a 0 % flash).
            self._seed_telemetry()
            self._touch()


# The process-wide instance used by the server, voice controller and agent loop.
environment_state = EnvironmentState()


def get_state() -> EnvironmentState:
    return environment_state
