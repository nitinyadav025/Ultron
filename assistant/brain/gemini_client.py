"""Single adapter for the google-genai SDK (generateContent + function calling).
Supports live switching of Model ID and ThinkingConfig level (low, medium, high).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import errors, types

from assistant.config import LOGS_DIR, VALID_THINKING_LEVELS, AssistantConfig
from assistant.safety.redact import redact_secrets

logger = logging.getLogger("ultron.gemini")
USAGE_FILE = LOGS_DIR / "usage.json"


@dataclass
class TokenUsageStats:
    """Token usage for a single task and cumulative daily total."""

    prompt_tokens: int = 0
    candidate_tokens: int = 0
    thought_tokens: int = 0
    total_tokens: int = 0
    daily_total: int = 0
    daily_cap: int = 500000

    @property
    def cap_percent(self) -> float:
        return round((self.daily_total / self.daily_cap) * 100.0, 1) if self.daily_cap > 0 else 0.0


class UsageMeter:
    """Persists daily token usage in logs/usage.json and warns at 80% cap."""

    def __init__(self, daily_cap: int = 500000, usage_path: Path | None = None) -> None:
        self.daily_cap = daily_cap
        self.usage_path = usage_path or USAGE_FILE

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict[str, int]:
        if not self.usage_path.exists():
            return {}
        try:
            return json.loads(self.usage_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def record(self, prompt_t: int, cand_t: int, thought_t: int, total_t: int) -> TokenUsageStats:
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load()
        day = self._today()
        computed = total_t or (prompt_t + cand_t + thought_t)
        data[day] = data.get(day, 0) + computed
        self.usage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return TokenUsageStats(prompt_t, cand_t, thought_t, computed, data[day], self.daily_cap)

    def get_today_total(self) -> int:
        return self._load().get(self._today(), 0)


def validate_model_id(model_id: str) -> None:
    """Reject empty or retired gemini-2.x models."""
    if not model_id or not model_id.strip():
        raise ValueError("Model ID is missing. Check your .env file.")
    if model_id.strip().lower().startswith(("gemini-2.", "models/gemini-2.")):
        raise ValueError(f"Model '{model_id}' is a retired gemini-2.x model and is forbidden.")


def normalize_thinking_level(level: str) -> str:
    """Validate thinking level is low, medium, or high."""
    cleaned = level.strip().lower()
    if cleaned not in VALID_THINKING_LEVELS:
        raise ValueError(f"Invalid thinking level '{level}'. Must be low, medium, or high.")
    return cleaned


class GeminiAdapter:
    """Adapter around google.genai.Client using generate_content."""

    def __init__(self, config: AssistantConfig) -> None:
        self.config = config
        self.usage_meter = UsageMeter(daily_cap=config.daily_token_cap)
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not self.config.has_valid_key:
                raise RuntimeError("GEMINI_API_KEY is missing or set to placeholder in .env.")
            self._client = genai.Client(api_key=self.config.api_key)
        return self._client

    def select_model(self, role: str = "main") -> str:
        """Return the active model (respecting live Dashboard/CLI override)."""
        if role == "main" and self.config.active_model_override:
            model_id = self.config.active_model_override
        else:
            if role == "planner" and not self.config.use_planner_model:
                role = "main"
            model_id = self.config.models.get(role) or self.config.current_model
        validate_model_id(model_id)
        return model_id

    def list_available_models(self) -> list[str]:
        names: list[str] = []
        for m in self.client.models.list():
            name = (getattr(m, "name", "") or "").removeprefix("models/")
            if name:
                names.append(name)
        return names

    def generate_turn(
        self,
        contents: list[types.Content],
        system_instruction: str,
        tools: list[types.Tool] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 3,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        """Call generate_content with ThinkingConfig and exponential backoff on 429/5xx."""
        model_id = self.select_model(role)
        level = normalize_thinking_level(thinking_level or self.config.active_thinking_level)

        # Attach ThinkingConfig for models supporting tunable thinking_level
        thinking_cfg = None if "live" in model_id and "extended" not in model_id else types.ThinkingConfig(thinking_level=level)
        gen_config = types.GenerateContentConfig(
            system_instruction=redact_secrets(system_instruction),
            thinking_config=thinking_cfg,
            tools=tools if tools else None,
        )

        delay = 2.0
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.client.models.generate_content(model=model_id, contents=contents, config=gen_config)
                u = getattr(resp, "usage_metadata", None)
                stats = self.usage_meter.record(
                    getattr(u, "prompt_token_count", 0) or 0,
                    getattr(u, "candidates_token_count", 0) or 0,
                    getattr(u, "thoughts_token_count", 0) or 0,
                    getattr(u, "total_token_count", 0) or 0,
                )
                return resp, stats
            except errors.APIError as exc:
                code = getattr(exc, "code", 0) or 0
                if (code == 429 or code >= 500) and attempt < max_retries:
                    logger.warning("Gemini HTTP %s (attempt %d/%d); retrying in %.1fs", code, attempt, max_retries, delay)
                    time.sleep(delay)
                    delay *= 2.0
                    continue
                if code == 429:
                    raise RuntimeError("Free-tier rate limit reached (HTTP 429). Wait 60s or switch to gemini-3.5-flash-lite in the Dashboard.") from exc
                raise
        raise RuntimeError("Gemini API call failed after retries.")
