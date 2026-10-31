"""Compact tool results for the local backend.

`BACKEND=local` sent every tool result twice (the wrapped `data` text *and* the full
`raw_summary`) with 100 entries, sizes and timestamps. On a `list_files` turn that pushed
the prompt to ~5,700 tokens and invited the model to restate details (the "9 TB" bug).

This module turns one raw tool result into a short, plain, fenced summary:
- `list_files`: first 15 names plus the total count, no sizes, no paths, no timestamps.
- `read_file`: the first few hundred characters and nothing else.
- everything else: a small JSON object, truncated.
"""

from __future__ import annotations

import json
from typing import Any

from assistant.tools.registry import wrap_untrusted_output

MAX_LIST_ENTRIES = 15
MAX_TEXT_CHARS = 400
MAX_GENERIC_CHARS = 400

# Fields that must never reach the model: it likes to parrot them back as facts.
_DROP_FIELDS = ("size_bytes", "modified", "path", "directory", "data", "raw_summary")


def _compact_listing(result: dict[str, Any], max_entries: int) -> str:
    items = result.get("items") or []
    total = int(result.get("count") or len(items))
    names: list[str] = []
    for entry in items[:max_entries]:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        names.append(f"{name}/" if entry.get("type") == "dir" else name)
    summary = f"{total} item(s) in folder. First {len(names)}: " + ", ".join(names)
    if len(names) < total:
        summary += f" … {total - len(names)} more not shown"
    return summary


def compact_tool_result(tool_name: str, result: dict[str, Any], max_entries: int = MAX_LIST_ENTRIES) -> str:
    """Render one tool result as a short fenced string for the local model."""
    if not result.get("ok", False):
        error = str(result.get("error") or result.get("reason") or "tool failed")[:200]
        return wrap_untrusted_output(tool_name, f"error: {error}", max_chars=MAX_GENERIC_CHARS)

    raw = result.get("raw_summary")
    if not isinstance(raw, dict):
        raw = {}
    if not raw:
        # Some callers pass the tool output straight through without raw_summary.
        raw = {k: v for k, v in result.items() if k not in ("ok", "data")}

    if tool_name == "list_files" and "items" in raw:
        text = _compact_listing(raw, max_entries)
    elif tool_name == "read_file" and "content" in raw:
        content = str(raw.get("content") or "")
        note = " (truncated)" if raw.get("truncated") or len(content) > MAX_TEXT_CHARS else ""
        text = f"content{note}: {content[:MAX_TEXT_CHARS]}"
    else:
        keep = {k: v for k, v in raw.items() if k not in _DROP_FIELDS}
        text = json.dumps(keep, ensure_ascii=False, default=str)
        if len(text) > MAX_GENERIC_CHARS:
            text = text[:MAX_GENERIC_CHARS] + "…"
    return wrap_untrusted_output(tool_name, text, max_chars=MAX_GENERIC_CHARS + 200)
