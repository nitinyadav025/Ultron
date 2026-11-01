"""Phase 1 Safe File Tools: list_files and read_file using pathlib."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant.safety.redact import redact_secrets
from assistant.tools.registry import tool

# Spoken folder names: "my Downloads folder", "documents", "desktop".
_SPOKEN_PREFIX = re.compile(r"(?i)^(?:my|the|in|from)\s+")
_SPOKEN_SUFFIX = re.compile(r"(?i)\s+(?:folder|directory|dir)$")
# Whisper often drops the plural: "download" must still mean ~/Downloads.
_SPOKEN_ALIASES = {
    "download": "Downloads",
    "downloads": "Downloads",
    "document": "Documents",
    "documents": "Documents",
    "desktop": "Desktop",
}


def resolve_user_directory(directory: str) -> Path:
    """Turn a spoken folder name into a real path (bare names resolve against $HOME).

    Voice input produces 'my Downloads folder' or 'Downloads', which must not be taken
    relative to the project directory. Bare names only ever map to $HOME, so the
    PermissionGate decision (home is inside ALLOWED_ROOTS) is unchanged.
    """
    cleaned = (directory or ".").strip().strip('"').strip("'")
    for _ in range(2):  # "in my downloads folder" has two leading words to drop
        cleaned = _SPOKEN_PREFIX.sub("", cleaned)
    cleaned = _SPOKEN_SUFFIX.sub("", cleaned).strip() or "."
    lowered = cleaned.lower()
    if lowered in ("home", "~"):
        return Path.home()
    path = Path(cleaned).expanduser()
    if path.is_absolute() or cleaned.startswith(".") or "/" in cleaned:
        return path
    return Path.home() / _SPOKEN_ALIASES.get(lowered, path.name)


def _collect_entries(target: Path, pattern: str, recursive: bool) -> list[dict[str, Any]]:
    iterator = target.rglob(pattern) if recursive else target.glob(pattern)
    items: list[dict[str, Any]] = []
    for entry in sorted(iterator, key=lambda p: (not p.is_dir(), p.name.lower())):
        if entry.name in (".git", "__pycache__", ".venv"):
            continue
        try:
            st = entry.stat()
            items.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "type": "dir" if entry.is_dir() else "file",
                    "size_bytes": st.st_size if entry.is_file() else 0,
                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        except OSError:
            continue
        if len(items) >= 100:
            break
    return items


@tool(risk_level="safe", read_only=True)
def list_files(directory: str = ".", pattern: str = "*", recursive: bool = False) -> dict[str, Any]:
    """List a folder's files and folders."""
    target = resolve_user_directory(directory).resolve()
    if not target.exists() or not target.is_dir():
        return {"ok": False, "error": f"Directory not found: {target}"}

    items = _collect_entries(target, pattern, recursive)
    # A pattern the local model invented ("*down*" for a garbled "download") used to make
    # a full folder look empty. If the filter hides everything, list the folder itself.
    if not items and pattern not in ("*", "", None):
        items = _collect_entries(target, "*", False)
    return {"ok": True, "directory": str(target), "count": len(items), "items": items}


@tool(risk_level="safe", read_only=True)
def read_file(path: str, max_chars: int = 4000) -> dict[str, Any]:
    """Read a text file."""
    target = Path(path).expanduser().resolve()
    if target.name == ".env" or (target.name.startswith(".env.") and target.name != ".env.example"):
        return {"ok": False, "error": "Refused: Reading .env is forbidden by safety rules."}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": f"File not found: {target}"}
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    truncated = len(raw) > max_chars
    return {
        "ok": True,
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "truncated": truncated,
        "content": redact_secrets(raw[:max_chars]),
    }
