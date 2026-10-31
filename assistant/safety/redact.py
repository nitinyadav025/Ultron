"""Secret redactor ensuring API keys, tokens, and passwords never leak to Gemini or logs."""

from __future__ import annotations

import os
import re

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED_GEMINI_KEY]"),
    (re.compile(r"sk-[0-9A-Za-z_\-]{20,}"), "[REDACTED_SK_KEY]"),
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[0-9A-Za-z_\-\.]{16,}"), r"\1[REDACTED_BEARER_TOKEN]"),
    (
        re.compile(r"(?i)(gemini_api_key|api_key|secret_key|password|passwd|access_token)\s*[:=]\s*['\"]?([^\s'\";]+)"),
        r"\1=[REDACTED_SECRET]",
    ),
]


def redact_secrets(text: str, extra_secrets: list[str] | None = None) -> str:
    """Redact anything matching an API key, token, or password."""
    if not text:
        return text
    redacted = text
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    candidates = list(extra_secrets or [])
    if env_key and env_key != "your_gemini_api_key_here" and len(env_key) >= 8:
        candidates.append(env_key)
    for secret in candidates:
        if secret and len(secret) >= 8 and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED_ENV_KEY]")
    for pat, repl in _SECRET_PATTERNS:
        redacted = pat.sub(repl, redacted)
    return redacted


def run_redactor_self_test() -> tuple[bool, str]:
    """Self-test planting a fake Gemini key and verifying complete redaction."""
    planted = "AIza" + "SyD9876543210abcdefghijkLMNOPQRSTUV"
    sample = f"Config leak test: GEMINI_API_KEY={planted}"
    cleaned = redact_secrets(sample)
    if planted in cleaned or "[REDACTED" not in cleaned:
        return False, "Redactor failed to mask planted key."
    return True, "Redactor caught planted Gemini key."
