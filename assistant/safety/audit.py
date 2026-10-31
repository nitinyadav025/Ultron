"""JSONL Audit Logger for every tool action and permission decision."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant.config import LOGS_DIR
from assistant.safety.redact import redact_secrets

AUDIT_FILE = LOGS_DIR / "audit.jsonl"


def _sanitize(val: Any) -> Any:
    if isinstance(val, str):
        return redact_secrets(val)
    if isinstance(val, dict):
        return {str(k): _sanitize(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_sanitize(v) for v in val]
    return val


def log_audit_event(
    user_request: str,
    tool_name: str,
    arguments: dict[str, Any],
    risk_level: str,
    mode: str,
    approved: bool,
    result: Any,
    duration_ms: float = 0.0,
    audit_path: Path | None = None,
) -> None:
    """Append one JSONL line per action with secrets redacted."""
    target = audit_path or AUDIT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    res_str = json.dumps(_sanitize(result), ensure_ascii=False)
    if len(res_str) > 600:
        res_str = res_str[:600] + "...[truncated]"
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "request": redact_secrets(user_request),
        "tool": tool_name,
        "arguments": _sanitize(arguments),
        "risk_level": risk_level,
        "mode": mode,
        "approved": approved,
        "duration_ms": round(duration_ms, 2),
        "result": res_str,
    }
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_audit_logs(limit: int = 30, audit_path: Path | None = None) -> list[dict[str, Any]]:
    """Read recent JSONL audit entries."""
    target = audit_path or AUDIT_FILE
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines()[-limit:]:
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out
