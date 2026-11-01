"""Spoken replies that are built from the tool result instead of from model wording.

The local 1.7B model answered a real ``list_files`` call with

    "Here are the first items in your download folder."

although the tool result already held the count and the names. Facts that code already
knows are therefore spoken from code. The model's sentence is only used when it carries
the answer (the guard below rejects pure lead-ins).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Openers that announce an answer instead of giving it.
_LEAD_IN_PREFIXES = (
    "here are",
    "here is",
    "here's",
    "sure",
    "okay",
    "ok",
    "certainly",
    "of course",
    "i found",
    "i have",
    "i've",
    "these are",
    "the first",
    "all set",
    "done",
)
_DIGIT = re.compile(r"\d")
_EMPTY_CLAIM = re.compile(r"(?i)\b(?:no items|no files|is empty|are empty|nothing in)\b")
_MAX_LEAD_IN_WORDS = 14
FOLDER_NOT_FOUND_REPLY = "I couldn't find that folder."


def folder_display_name(directory: Any) -> str:
    """Return the spoken folder name: '/home/you/Downloads' -> 'Downloads', home -> 'home'."""
    if not directory:
        return "home"
    path = Path(str(directory)).expanduser()
    try:
        if path == Path.home():
            return "home"
    except OSError:  # pragma: no cover - Path.home() rarely fails
        pass
    return path.name or "home"


def list_files_reply(result: dict[str, Any]) -> str | None:
    """Build the spoken listing sentence from the real tool result.

    A failed call (missing folder, outside the allowed area, denied) always says the
    folder was not found. "No items" is only possible from a real, empty listing.
    """
    if not result.get("ok", False):
        return FOLDER_NOT_FOUND_REPLY
    raw = result.get("raw_summary")
    if not isinstance(raw, dict):
        raw = result
    items = raw.get("items")
    if not isinstance(items, list):
        return None
    try:
        count = int(raw.get("count") if raw.get("count") is not None else len(items))
    except (TypeError, ValueError):
        count = len(items)
    folder = folder_display_name(raw.get("directory"))
    if count <= 0:
        return f"There are no items in your {folder} folder."
    noun = "item" if count == 1 else "items"
    verb = "is" if count == 1 else "are"
    return f"There {verb} {count} {noun} in your {folder} folder."


def claims_an_empty_folder(text: str) -> bool:
    """True when a sentence claims a folder is empty (must be backed by a real listing)."""
    return bool(_EMPTY_CLAIM.search(text or ""))


def is_lead_in(text: str) -> bool:
    """True for a contentless opener such as "Here are the first items in your folder".

    A real answer almost always contains a number ("12 items"), and long sentences
    normally carry the answer, so only short, number-free openers are rejected.
    """
    cleaned = re.sub(r"\s+", " ", (text or "")).strip(" .:!?\"'")
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if _DIGIT.search(lowered):
        return False
    if len(lowered.split()) > _MAX_LEAD_IN_WORDS:
        return False
    return lowered.startswith(_LEAD_IN_PREFIXES)


def code_reply_for_tool(tool_name: str, result: dict[str, Any]) -> str | None:
    """Deterministic reply for tools whose answer is already known in code."""
    if tool_name == "list_files":
        return list_files_reply(result)
    return None


def fallback_reply(tool_name: str) -> str:
    """Short safe sentence when the model only produced a lead-in for ``tool_name``."""
    return {
        "list_files": "I listed the folder.",
        "read_file": "I read the file.",
        "open_app": "Done.",
    }.get(tool_name, "Done.")
