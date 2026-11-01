"""Fast path: answer simple whole-sentence commands without the LLM.

Only commands the registered tools already support are matched. Anything unclear falls
through to the normal agent loop (the LLM) unchanged. Tools still run through
``ToolRegistry.execute``, so permission modes apply exactly as before.

"close <app>" is recognised but not implemented: there is no close tool in this project,
so the reply says so honestly and nothing is run. It never goes to the LLM, which used to
invent an action.
"""

from __future__ import annotations

import logging
import re

from assistant.brain.tool_replies import code_reply_for_tool
from assistant.tools.registry import ToolRegistry
from assistant.tools.volume import numbers_in_text

logger = logging.getLogger("ultron.fastpath")

CLOSE_REPLY = "I can't close apps yet."


def _normalise(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _single_number(text: str) -> int | None:
    values = numbers_in_text(text)
    if len(values) != 1:
        return None
    return next(iter(values))


def _clean_target(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .")


def match_fast_command(text: str) -> dict[str, object] | None:
    """Return {"kind", "tool", "arguments", "request"} for a simple whole sentence."""
    cleaned = _normalise(text)
    if not cleaned:
        return None

    patterns: list[tuple[str, re.Pattern[str]]] = [
        # one step: open the browser with the search URL
        (
            "web_search",
            re.compile(r"^(?:please )?open (?P<browser>[a-z0-9 ]+?) and search (?:the web )?for (?P<query>.+)$"),
        ),
        ("open_first_result", re.compile(r"^(?:please )?open (?:the )?first result (?:for|of) (?P<query>.+)$")),
        ("open_first_result", re.compile(r"^(?:please )?search (?:for )?(?P<query>.+?) and open (?:the )?first (?:link|result)$")),
        ("web_search", re.compile(r"^(?:please )?search (?:for )?(?P<query>.+?) in (?P<browser>[a-z0-9 ]+)$")),
        ("web_search", re.compile(r"^(?:please )?search (?:for )?(?P<query>.+)$")),
        ("open_app", re.compile(r"^(?:please )?(?:open|launch) (?P<app>.+)$")),
        ("close_app", re.compile(r"^(?:please )?(?:close|quit) (?P<app>.+)$")),
        ("mute", re.compile(r"^(?:please )?(?:mute|mute the audio|mute the sound)$")),
        ("unmute", re.compile(r"^(?:please )?(?:unmute|unmute the audio|unmute the sound)$")),
        ("set_volume", re.compile(r"^(?:please )?(?:turn|set|change) (?:the )?volume to (?P<value>.+)$")),
        ("list_files", re.compile(r"^(?:please )?(?:list|show) (?:the )?files (?:in|from) (?P<folder>.+)$")),
    ]

    for kind, pattern in patterns:
        match = pattern.match(cleaned)
        if not match:
            continue
        groups = match.groupdict()
        if kind == "close_app":
            return {"kind": kind, "tool": None, "arguments": {"app_name": groups["app"]}, "request": text}
        if kind == "open_app":
            app = _clean_target(groups["app"])
            if " and " in f" {app} " or " then " in f" {app} ":
                return None  # a multi-step sentence belongs to the LLM
            return {"kind": kind, "tool": "open_app", "arguments": {"app_name": app}, "request": text}
        if kind == "mute":
            return {"kind": kind, "tool": "mute_audio", "arguments": {"muted": True}, "request": text}
        if kind == "unmute":
            return {"kind": kind, "tool": "mute_audio", "arguments": {"muted": False}, "request": text}
        if kind == "set_volume":
            percent = _single_number(groups["value"])
            if percent is None:
                return None
            return {"kind": kind, "tool": "set_volume", "arguments": {"percent": percent}, "request": text}
        if kind == "list_files":
            folder = _clean_target(groups["folder"])
            folder = re.sub(r"^(?:my|the) ", "", folder)
            folder = re.sub(r" (?i:folder|directory)$", "", folder).strip()
            if not folder or " and " in f" {folder} ":
                return None
            return {"kind": kind, "tool": "list_files", "arguments": {"directory": folder}, "request": text}
        if kind == "web_search":
            query = _clean_target(groups["query"])
            browser = _clean_target(groups.get("browser") or "")
            if not query:
                return None
            return {
                "kind": kind,
                "tool": "web_search",
                "arguments": {"query": query, "browser": browser or None},
                "request": text,
            }
        if kind == "open_first_result":
            query = _clean_target(groups["query"])
            if not query:
                return None
            return {"kind": kind, "tool": "open_first_result", "arguments": {"query": query}, "request": text}
    return None


def run_fast_command(registry: ToolRegistry, command: dict[str, object]) -> str:
    """Run the matched command through the registry and build the reply in code."""
    kind = str(command["kind"])
    tool_name = command.get("tool")
    arguments = dict(command.get("arguments") or {})
    request = str(command.get("request") or "")
    if tool_name is None:
        return CLOSE_REPLY
    result = registry.execute(str(tool_name), arguments, request)
    reply = code_reply_for_tool(str(tool_name), result)
    return reply or "Done."


def try_fast_path(registry: ToolRegistry, text: str) -> str | None:
    """Return a code-built reply, or None when the sentence must go to the LLM."""
    command = match_fast_command(text)
    if command is None:
        logger.info("Fast path: LLM used for %r", text)
        return None
    logger.info("Fast path: used for %r (%s)", text, command["kind"])
    return run_fast_command(registry, command)
