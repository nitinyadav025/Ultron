"""Configuration loader for Ultron. Reads .env and config/settings.yaml and manages live Model/Thinking state."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The test suite points these two at a temporary folder so no test writes into the real
# logs/ or workspace/ (see tests/conftest.py). Normal runs use the project folders.
LOGS_DIR = Path(os.environ.get("ULTRON_LOGS_DIR") or (PROJECT_ROOT / "logs"))
WORKSPACE_DIR = Path(os.environ.get("ULTRON_WORKSPACE_DIR") or (PROJECT_ROOT / "workspace"))
BACKUPS_DIR = PROJECT_ROOT / ".backups"
MODELS_DIR = PROJECT_ROOT / "models"

VALID_THINKING_LEVELS = ("low", "medium", "high")


def setup_logging() -> logging.Logger:
    """Configure file logging to logs/assistant.log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ultron")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(LOGS_DIR / "assistant.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(fh)
    return logger


def clamp_voice_volume(val: float | str) -> float:
    """Clamp VOICE_VOLUME to the safe [0.5, 2.0] range."""
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        parsed = 1.0
    return max(0.5, min(2.0, parsed))


@dataclass
class AssistantConfig:
    """Runtime configuration state supporting live model and thinking-level switching."""

    project_root: Path = PROJECT_ROOT
    assistant_name: str = "Ultron"
    backend: str = "local"  # "local" (llama.cpp) or "gemini" (cloud rollback)
    llama_base_url: str = "http://127.0.0.1:9931/v1"
    llama_server_binary: str = ""
    llama_model_path: str = ""
    llama_timeout_sec: float = 60.0
    llama_start_timeout_sec: float = 90.0
    llama_server_log: Path = LOGS_DIR / "llama-server.log"
    whisper_model: str = "base.en"
    whisper_model_dir: Path = MODELS_DIR / "faster-whisper-base.en"
    whisper_hotwords: str = ""
    whisper_min_avg_logprob: float = -1.0
    whisper_max_no_speech_prob: float = 0.60
    whisper_max_compression_ratio: float = 2.4
    mic_source_volume: str = ""
    mic_source_name: str = ""
    piper_voice_dir: Path = MODELS_DIR / "piper"
    piper_voice: str = "en_US-lessac-medium"
    api_key: str = ""
    paid_tier: bool = False
    models: dict[str, str] = field(default_factory=dict)
    active_model_override: str = ""
    active_thinking_level: str = "medium"
    use_planner_model: bool = False
    tts_voice: str = "Kore"
    tts_fallback_voice_type: str = "male1"
    tts_fallback_pitch: int = 15
    tts_fallback_rate: int = 10
    voice_volume: float = 1.0
    voice_reply_max_chars: int = 160
    # Microphone capture: "auto" listens until the user stops talking; "fixed" records a
    # fixed window (the old behaviour) for VOICE_FIXED_SECONDS.
    voice_mode: str = "auto"
    voice_start_timeout_s: float = 10.0
    voice_silence_ms: int = 2000
    voice_max_seconds: float = 30.0
    voice_fixed_seconds: float = 5.0
    voice_debug: bool = False
    recorder_warmup_ms: int = 300
    recorder_warmup_max_ms: int = 1500
    voice_min_snr_db: float = 10.0
    voice_highpass_hz: float = 100.0
    # OFF by default: the Nov 1 A/B showed no gain and one clean clip got worse.
    # Set VOICE_DENOISE_STRENGTH=0.6 (max 0.8) to try it.
    voice_denoise_strength: float = 0.0
    wake_word: str = "ultron"
    wake_aliases: str = ""
    wake_aliases_file: Path = WORKSPACE_DIR / "wake_aliases.json"
    listen_followup_s: float = 6.0
    listen_early_check_ms: int = 800
    listen_after_speech_ms: int = 300
    listen_log_ignored: bool = False
    listen_debug: bool = False
    daily_token_cap: int = 500000
    save_screenshots: bool = False
    default_mode: str = "auto-safe"
    max_steps: int = 25
    wall_clock_timeout_sec: int = 180
    idle_standdown_sec: int = 30
    max_repeated_failures: int = 3
    max_tool_output_chars: int = 4000
    confirmation_timeout_sec: int = 60
    thinking_levels: dict[str, str] = field(default_factory=dict)
    allowed_roots: list[Path] = field(default_factory=list)
    readonly_system_dirs: list[Path] = field(default_factory=list)
    kill_hotkey_binding: str = "<Ctrl><Alt>q"
    sandbox_memory_max: str = "512M"
    sandbox_cpu_quota: str = "25%"
    sandbox_timeout_sec: int = 30
    sandbox_use_bwrap: bool = False

    @property
    def has_valid_key(self) -> bool:
        """Return True if a real API key is configured."""
        return bool(self.api_key and self.api_key != "your_gemini_api_key_here" and len(self.api_key.strip()) > 10)

    @property
    def voice_live_enabled(self) -> bool:
        """Gemini Live voice is cloud-only; it stays switched off on the local backend."""
        return self.backend != "local"

    @property
    def local_model_name(self) -> str:
        """Human-readable label for the single local llama.cpp model."""
        if self.llama_model_path:
            return Path(self.llama_model_path).stem
        return "local-llm"

    @property
    def current_model(self) -> str:
        """Return the active model label (local llama.cpp model or Gemini model ID)."""
        if self.backend == "local":
            return self.local_model_name
        return self.active_model_override or self.models.get("main", "")

    def available_model_choices(self) -> list[str]:
        """Return selectable model IDs (a single fixed model when BACKEND=local)."""
        if self.backend == "local":
            return [self.local_model_name]
        choices: list[str] = []
        for role in ("main", "fast", "planner", "computer_use", "voice_live", "transcribe", "tts"):
            m = self.models.get(role, "").strip()
            if m and m not in choices:
                choices.append(m)
        return choices

    def set_active_model(self, model_id: str) -> str:
        """Switch the active Gemini model at runtime (local backend has a single fixed model)."""
        if self.backend == "local":
            raise ValueError("BACKEND=local uses one fixed llama.cpp model; model switching is disabled.")
        cleaned = model_id.strip()
        if not cleaned:
            raise ValueError("Model ID cannot be empty.")
        if cleaned.lower().startswith(("gemini-2.", "models/gemini-2.")):
            raise ValueError(f"Model '{cleaned}' is retired (gemini-2.x is forbidden).")
        self.active_model_override = cleaned
        return self.active_model_override

    def set_thinking_level(self, level: str) -> str:
        """Switch the active thinking level (low, medium, or high) at runtime."""
        cleaned = level.strip().lower()
        if cleaned not in VALID_THINKING_LEVELS:
            raise ValueError(f"Invalid thinking level '{level}'. Choose from: low, medium, high.")
        self.active_thinking_level = cleaned
        return self.active_thinking_level


def load_config(env_path: Path | None = None, settings_path: Path | None = None) -> AssistantConfig:
    """Load configuration from .env (with .env.example fallback) and config/settings.yaml."""
    setup_logging()
    env_file = env_path or (PROJECT_ROOT / ".env")
    example_file = PROJECT_ROOT / ".env.example"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    merged: dict[str, str] = {}
    for src in (example_file, env_file):
        if src.exists():
            for k, v in dotenv_values(src).items():
                if v is not None:
                    merged[k] = v
    for k, v in os.environ.items():
        if k.startswith(
            (
                "GEMINI_",
                "COMPUTER_USE_",
                "USE_PLANNER_",
                "DAILY_TOKEN_",
                "SAVE_SCREENSHOTS",
                "ASSISTANT_",
                "VOICE_",
                "BACKEND",
                "LLAMA_",
                "WHISPER_",
                "PIPER_",
                "MIC_",
            )
        ):
            merged[k] = v

    yaml_file = settings_path or (PROJECT_ROOT / "config" / "settings.yaml")
    raw_yaml: dict[str, Any] = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) if yaml_file.exists() else {}
    ast_cfg = raw_yaml.get("assistant", {})
    think_cfg = raw_yaml.get("thinking", {})
    safety_cfg = raw_yaml.get("safety", {})
    sandbox_cfg = raw_yaml.get("sandbox", {})

    allowed_roots: list[Path] = []
    if safety_cfg.get("allow_full_home_dir", True):
        allowed_roots.append(Path.home().resolve())
    for d in safety_cfg.get("allowed_xdg_dirs", ["Desktop", "Documents", "Downloads"]):
        p = (Path.home() / d).resolve()
        if p not in allowed_roots:
            allowed_roots.append(p)
    if safety_cfg.get("include_workspace", True):
        allowed_roots.append(WORKSPACE_DIR.resolve())

    readonly_dirs = [
        Path(p).resolve() for p in safety_cfg.get("readonly_system_dirs", ["/etc", "/usr", "/boot", "/proc", "/sys"])
    ]
    models = {
        "main": merged.get("GEMINI_MODEL", ""),
        "computer_use": merged.get("COMPUTER_USE_MODEL", ""),
        "fast": merged.get("GEMINI_FAST_MODEL", ""),
        "planner": merged.get("GEMINI_PLANNER_MODEL", ""),
        "voice_live": merged.get("GEMINI_VOICE_LIVE_MODEL", ""),
        "transcribe": merged.get("GEMINI_TRANSCRIBE_MODEL", ""),
        "tts": merged.get("GEMINI_TTS_MODEL", ""),
    }
    default_thinking = (merged.get("GEMINI_THINKING_LEVEL") or think_cfg.get("default_level", "medium")).lower()
    if default_thinking not in VALID_THINKING_LEVELS:
        default_thinking = "medium"

    tts_voice = (merged.get("GEMINI_TTS_VOICE") or "Kore").strip() or "Kore"
    tts_fallback_voice_type = (merged.get("VOICE_FALLBACK_VOICE_TYPE") or "male1").strip().lower() or "male1"
    tts_fallback_pitch = max(-100, min(100, int(merged.get("VOICE_FALLBACK_PITCH") or "15")))
    tts_fallback_rate = max(-100, min(100, int(merged.get("VOICE_FALLBACK_RATE") or "10")))
    voice_vol = clamp_voice_volume(merged.get("VOICE_VOLUME", "1.0"))
    try:
        voice_reply_max_chars = max(40, min(400, int(merged.get("VOICE_REPLY_MAX_CHARS") or "160")))
    except (TypeError, ValueError):
        voice_reply_max_chars = 160

    voice_mode = (merged.get("VOICE_MODE") or "auto").strip().lower()
    if voice_mode not in ("auto", "fixed"):
        voice_mode = "auto"

    def _positive_float(key: str, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(high, float(merged.get(key) or default)))
        except (TypeError, ValueError):
            return default

    def _positive_int(key: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(merged.get(key) or default)))
        except (TypeError, ValueError):
            return default

    def _float_value(key: str, default: float) -> float:
        try:
            return float(merged.get(key) or default)
        except (TypeError, ValueError):
            return default

    voice_start_timeout_s = _positive_float("VOICE_START_TIMEOUT_S", 10.0, 1.0, 60.0)
    voice_silence_ms = _positive_int("VOICE_SILENCE_MS", 2000, 500, 10000)
    voice_max_seconds = _positive_float("VOICE_MAX_SECONDS", 30.0, 3.0, 120.0)
    voice_fixed_seconds = _positive_float("VOICE_FIXED_SECONDS", 5.0, 1.0, 60.0)
    voice_debug = merged.get("VOICE_DEBUG", "false").lower() == "true"
    recorder_warmup_ms = _positive_int("RECORDER_WARMUP_MS", 300, 0, 2000)
    recorder_warmup_max_ms = _positive_int("RECORDER_WARMUP_MAX_MS", 1500, 300, 5000)
    voice_min_snr_db = _positive_float("VOICE_MIN_SNR_DB", 10.0, 0.0, 30.0)
    voice_highpass_hz = _positive_float("VOICE_HIGHPASS_HZ", 100.0, 0.0, 800.0)
    try:
        voice_denoise_strength = max(0.0, min(0.8, float(merged.get("VOICE_DENOISE_STRENGTH") or "0.0")))
    except (TypeError, ValueError):
        voice_denoise_strength = 0.0
    wake_word = (merged.get("WAKE_WORD") or "ultron").strip().lower() or "ultron"
    wake_aliases = (merged.get("WAKE_ALIASES") or "").strip()
    wake_aliases_file = Path(os.path.expanduser(merged.get("WAKE_ALIASES_FILE") or str(WORKSPACE_DIR / "wake_aliases.json")))
    listen_followup_s = _positive_float("LISTEN_FOLLOWUP_S", 6.0, 0.0, 60.0)
    listen_early_check_ms = _positive_int("LISTEN_EARLY_CHECK_MS", 800, 200, 5000)
    listen_after_speech_ms = _positive_int("LISTEN_AFTER_SPEECH_MS", 300, 0, 5000)
    listen_log_ignored = merged.get("LISTEN_LOG_IGNORED", "0").strip().lower() in ("1", "true", "yes")
    listen_debug = merged.get("LISTEN_DEBUG", "0").strip().lower() in ("1", "true", "yes")

    backend = (merged.get("BACKEND") or "local").strip().lower()
    if backend not in ("local", "gemini"):
        backend = "local"
    llama_base_url = (merged.get("LLAMA_BASE_URL") or "http://127.0.0.1:9931/v1").strip().rstrip("/")
    llama_server_binary = os.path.expanduser(
        merged.get("LLAMA_SERVER_BIN")
        or str(Path.home() / "Downloads/LLAMA/llama-b12001-bin-ubuntu-cuda-13.4-x64/llama-b12001/llama-server")
    )
    llama_model_path = os.path.expanduser(
        merged.get("LLAMA_MODEL_PATH") or str(Path.home() / "Downloads/LFM3-1.7B-Q6_K.gguf")
    )
    try:
        llama_timeout_sec = max(5.0, float(merged.get("LLAMA_TIMEOUT_SEC") or "60"))
    except (TypeError, ValueError):
        llama_timeout_sec = 60.0
    try:
        llama_start_timeout_sec = max(5.0, float(merged.get("LLAMA_START_TIMEOUT_SEC") or "90"))
    except (TypeError, ValueError):
        llama_start_timeout_sec = 90.0
    llama_server_log = Path(
        os.path.expanduser(merged.get("LLAMA_LOG_PATH") or str(LOGS_DIR / "llama-server.log"))
    )
    whisper_model = (merged.get("WHISPER_MODEL") or "base.en").strip() or "base.en"
    whisper_model_dir = Path(
        os.path.expanduser(
            merged.get("WHISPER_MODEL_DIR") or str(MODELS_DIR / f"faster-whisper-{whisper_model}")
        )
    )
    piper_voice = (merged.get("PIPER_VOICE") or "en_US-lessac-medium").strip() or "en_US-lessac-medium"
    piper_voice_dir = Path(os.path.expanduser(merged.get("PIPER_VOICE_DIR") or str(MODELS_DIR / "piper")))

    return AssistantConfig(
        project_root=PROJECT_ROOT,
        assistant_name=merged.get("ASSISTANT_NAME") or ast_cfg.get("name", "Ultron"),
        backend=backend,
        llama_base_url=llama_base_url,
        llama_server_binary=llama_server_binary,
        llama_model_path=llama_model_path,
        llama_timeout_sec=llama_timeout_sec,
        llama_start_timeout_sec=llama_start_timeout_sec,
        llama_server_log=llama_server_log,
        whisper_model=whisper_model,
        whisper_model_dir=whisper_model_dir,
        whisper_hotwords=(merged.get("WHISPER_HOTWORDS") or "").strip(),
        whisper_min_avg_logprob=_float_value("WHISPER_MIN_AVG_LOGPROB", -1.0),
        whisper_max_no_speech_prob=_float_value("WHISPER_MAX_NO_SPEECH_PROB", 0.60),
        whisper_max_compression_ratio=_float_value("WHISPER_MAX_COMPRESSION_RATIO", 2.4),
        mic_source_volume=(merged.get("MIC_SOURCE_VOLUME") or "").strip(),
        mic_source_name=(merged.get("MIC_SOURCE_NAME") or "").strip(),
        piper_voice_dir=piper_voice_dir,
        piper_voice=piper_voice,
        api_key=merged.get("GEMINI_API_KEY", ""),
        paid_tier=merged.get("GEMINI_PAID_TIER", "false").lower() == "true",
        models=models,
        active_thinking_level=default_thinking,
        use_planner_model=merged.get("USE_PLANNER_MODEL", "false").lower() == "true",
        tts_voice=tts_voice,
        tts_fallback_voice_type=tts_fallback_voice_type,
        tts_fallback_pitch=tts_fallback_pitch,
        tts_fallback_rate=tts_fallback_rate,
        voice_volume=voice_vol,
        voice_reply_max_chars=voice_reply_max_chars,
        voice_mode=voice_mode,
        voice_start_timeout_s=voice_start_timeout_s,
        voice_silence_ms=voice_silence_ms,
        voice_max_seconds=voice_max_seconds,
        voice_fixed_seconds=voice_fixed_seconds,
        voice_debug=voice_debug,
        recorder_warmup_ms=recorder_warmup_ms,
        recorder_warmup_max_ms=recorder_warmup_max_ms,
        voice_min_snr_db=voice_min_snr_db,
        voice_highpass_hz=voice_highpass_hz,
        voice_denoise_strength=voice_denoise_strength,
        wake_word=wake_word,
        wake_aliases=wake_aliases,
        wake_aliases_file=wake_aliases_file,
        listen_followup_s=listen_followup_s,
        listen_early_check_ms=listen_early_check_ms,
        listen_after_speech_ms=listen_after_speech_ms,
        listen_log_ignored=listen_log_ignored,
        listen_debug=listen_debug,
        daily_token_cap=int(merged.get("DAILY_TOKEN_CAP", "500000")),
        save_screenshots=merged.get("SAVE_SCREENSHOTS", "false").lower() == "true",
        default_mode=ast_cfg.get("default_mode", "auto-safe"),
        max_steps=int(ast_cfg.get("max_steps", 25)),
        wall_clock_timeout_sec=int(ast_cfg.get("wall_clock_timeout_sec", 180)),
        idle_standdown_sec=int(ast_cfg.get("idle_standdown_sec", 30)),
        max_repeated_failures=int(ast_cfg.get("max_repeated_failures", 3)),
        max_tool_output_chars=int(ast_cfg.get("max_tool_output_chars", 4000)),
        confirmation_timeout_sec=int(ast_cfg.get("confirmation_timeout_sec", 60)),
        thinking_levels={
            "default": think_cfg.get("default_level", "medium"),
            "planning": think_cfg.get("planning_level", "high"),
            "routing": think_cfg.get("routing_level", "low"),
        },
        allowed_roots=allowed_roots,
        readonly_system_dirs=readonly_dirs,
        kill_hotkey_binding=safety_cfg.get("kill_hotkey_binding", "<Ctrl><Alt>q"),
        sandbox_memory_max=sandbox_cfg.get("memory_max", "512M"),
        sandbox_cpu_quota=sandbox_cfg.get("cpu_quota", "25%"),
        sandbox_timeout_sec=int(sandbox_cfg.get("timeout_sec", 30)),
        sandbox_use_bwrap=bool(sandbox_cfg.get("use_bubblewrap_if_available", False)),
    )
