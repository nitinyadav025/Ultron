"""Fast path: answer simple whole-sentence commands without the LLM.

Only commands the registered tools already support are matched. Anything unclear falls
through to the normal agent loop (the LLM) unchanged. Tools still run through
``ToolRegistry.execute``, so permission modes apply exactly as before.

Priority matters: "open the first link for GitHub" is an ``open_first_result`` request,
never ``open_app``. "close firefox" (including conversational forms such as "Fire Fox is
open. Can you close the firefox for me?") calls the real ``close_app`` tool.
"""

from __future__ import annotations

import logging
import re

from assistant.brain.context import SESSION
from assistant.brain.tool_replies import code_reply_for_tool
from assistant.speech.wake import normalise_spoken_command
from assistant.tools.apps import app_key
from assistant.tools.registry import ToolRegistry
from assistant.tools.volume import numbers_in_text

logger = logging.getLogger("ultron.fastpath")

# Instant, tool-free replies for the most common conversational turns.
CHAT_REPLIES: dict[str, str] = {
    "hello": "Hello. I am here.",
    "hi": "Hello. I am here.",
    "hey": "Hello. I am here.",
    "hey ultron": "Hello. I am here.",
    "hello ultron": "Hello. I am here.",
    "good morning": "Good morning. I am ready.",
    "good evening": "Good evening. I am ready.",
    "what is your name": "I am Ultron, your local voice assistant.",
    "what s your name": "I am Ultron, your local voice assistant.",
    "whats your name": "I am Ultron, your local voice assistant.",
    "what is your name ultron": "I am Ultron, your local voice assistant.",
    "who are you": "I am Ultron, your local voice assistant.",
    "what are you": "I am Ultron, your local voice assistant.",
    "how are you": "I am running locally and ready to help.",
    "how is it going": "I am running locally and ready to help.",
    "thank you": "You're welcome.",
    "thanks": "You're welcome.",
    "thank you ultron": "You're welcome.",
    "are you there": "Yes, I am here.",
    "you there": "Yes, I am here.",
}


def _chat_reply(cleaned: str) -> str | None:
    return CHAT_REPLIES.get(cleaned)


def _chat(command_text: str, reply: str, request: str) -> dict[str, object]:
    return {"kind": "chat", "tool": None, "reply": reply, "request": request}


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


def _strip_article(value: str) -> str:
    return re.sub(r"^(?:the|a|an) ", "", _clean_target(value)).strip()


_REQUEST_PREFIX = re.compile(
    r"^(?:(?:yes|yeah|ok|okay)\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:tell\s+me\s+)?|"
    r"(?:i\s+want\s+you\s+to|i'd\s+like\s+you\s+to|tell\s+me|please)\s+)"
)


def _strip_request_prefixes(cleaned: str) -> str:
    for _ in range(3):
        stripped = _REQUEST_PREFIX.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


def match_fast_command(text: str) -> dict[str, object] | None:
    """Return {"kind", "tool", "arguments", "request"} for a simple whole sentence."""
    raw = (text or "").strip()
    typed = re.match(r"(?i)^(?:please )?(?:type|write|enter text) (.+)$", raw)
    if typed:
        return {
            "kind": "type_text",
            "tool": "type_text",
            "arguments": {"text": typed.group(1).strip()},
            "request": text,
        }

    cleaned = _strip_request_prefixes(_normalise(normalise_spoken_command(text)))
    if not cleaned:
        return None

    instant = _chat_reply(cleaned)
    if instant:
        return _chat(cleaned, instant, text)

    # Context follow-ups. These must run before the generic close/open patterns.
    if cleaned in {"close it", "quit it", "close that", "exit it", "kill it"}:
        if not SESSION.last_app:
            return _chat(cleaned, "I don't know what to close yet.", text)
        return {
            "kind": "close_app",
            "tool": "close_app",
            "arguments": {"app_name": SESSION.last_app},
            "request": text,
        }
    if cleaned in {
        "open the first link",
        "open first link",
        "open the first result",
        "open first result",
    }:
        if not SESSION.last_query:
            return _chat(cleaned, "What should I search for first?", text)
        return {
            "kind": "open_first_result",
            "tool": "open_first_result",
            "arguments": {
                "query": SESSION.last_query,
                "browser": SESSION.last_browser or None,
            },
            "request": text,
        }

    patterns: list[tuple[str, re.Pattern[str]]] = [
        # Browser tabs and navigation come before "open"/"close"/"switch" so a tab
        # phrase is never mistaken for an app name.
        ("tab:new_tab", re.compile(r"^(?:please )?(?:open )?(?:a )?new tab$")),
        ("tab:close_tab", re.compile(r"^(?:please )?close (?:this |the )?tab$")),
        (
            "tab:next_tab",
            re.compile(r"^(?:please )?(?:switch|jump) to (?:the )?next tab$"),
        ),
        (
            "tab:prev_tab",
            re.compile(r"^(?:please )?(?:switch|jump) to (?:the )?previous tab$"),
        ),
        ("tab:next_tab", re.compile(r"^(?:please )?next tab$")),
        ("tab:prev_tab", re.compile(r"^(?:please )?(?:previous|prev) tab$")),
        ("tab:reopen_tab", re.compile(r"^(?:please )?reopen (?:the )?(?:last )?tab$")),
        ("tab:refresh", re.compile(r"^(?:please )?refresh(?: the)?(?: page)?$")),
        ("tab:back", re.compile(r"^(?:please )?go back$")),
        ("tab:forward", re.compile(r"^(?:please )?go forward$")),
        # Window / workspace control.
        (
            "win:next_window",
            re.compile(r"^(?:please )?(?:switch|next) (?:to (?:the )?)?windows?$"),
        ),
        ("win:next_window", re.compile(r"^(?:please )?alt tab$")),
        ("win:next_window", re.compile(r"^(?:please )?jump to (?:the )?other window$")),
        ("win:maximize", re.compile(r"^(?:please )?maximize(?: the)? window$")),
        ("win:minimize", re.compile(r"^(?:please )?minimize(?: the)? window$")),
        ("win:fullscreen", re.compile(r"^(?:please )?(?:full ?screen)$")),
        (
            "win:workspace_next",
            re.compile(r"^(?:please )?(?:next workspace|slide right|workspace right)$"),
        ),
        (
            "win:workspace_prev",
            re.compile(r"^(?:please )?(?:previous workspace|prev workspace|slide left|workspace left)$"),
        ),
        (
            "win:overview",
            re.compile(r"^(?:please )?(?:show windows|overview|show overview)$"),
        ),
        # Scrolling.
        ("scroll:down", re.compile(r"^(?:please )?(?:scroll|slide) down$")),
        ("scroll:up", re.compile(r"^(?:please )?(?:scroll|slide) up$")),
        ("scroll:page_down", re.compile(r"^(?:please )?page down$")),
        ("scroll:page_up", re.compile(r"^(?:please )?page up$")),
        (
            "scroll:edge",
            re.compile(r"^(?:please )?scroll to (?:the )?(?P<edge>top|bottom)$"),
        ),
        # Brightness and media.
        ("brightness:up", re.compile(r"^(?:please )?(?:increase|brightness up)$")),
        ("brightness:down", re.compile(r"^(?:please )?(?:decrease|brightness down)$")),
        (
            "brightness:set",
            re.compile(r"^(?:please )?set brightness to (?P<value>.+)$"),
        ),
        ("media:play_pause", re.compile(r"^(?:please )?(?:play|pause|resume)(?: the)? music$")),
        ("media:play_pause", re.compile(r"^(?:please )?(?:play|pause|resume)$")),
        ("media:next", re.compile(r"^(?:please )?next (?:song|track)$")),
        ("media:previous", re.compile(r"^(?:please )?(?:previous|prev) (?:song|track)$")),
        # System information and screenshot.
        ("system:battery", re.compile(r"^(?:please )?(?:what is my battery|battery status)$")),
        ("system:time", re.compile(r"^(?:please )?(?:what time is it|what is the time)$")),
        (
            "system:date",
            re.compile(r"^(?:please )?(?:what is today s date|what date is it)$"),
        ),
        ("system:status", re.compile(r"^(?:please )?system status$")),
        ("screenshot", re.compile(r"^(?:please )?(?:take a screenshot|capture screen|screenshot)$")),
        # one step: open the browser with the search URL
        (
            "web_search",
            re.compile(r"^(?:please )?open (?P<browser>[a-z0-9 ]+?) and search (?:the web )?for (?P<query>.+)$"),
        ),
        # "open the first link for X" is never an app name (this was the live bug).
        (
            "open_first_result",
            re.compile(
                r"^(?:please )?open (?:the )?first (?:link|result) (?:for|of|on|in) (?P<query>.+)$"
            ),
        ),
        (
            "open_first_result",
            re.compile(
                r"^(?:please )?open (?:the )?(?P<query>.+?) and (?:then )?open (?:the )?first (?:link|result)$"
            ),
        ),
        (
            "open_first_result",
            re.compile(
                r"^(?:please )?search (?:for )?(?P<query>.+?) and open (?:the )?first (?:link|result)$"
            ),
        ),
        # Direct and conversational close requests, before any "open" pattern.
        (
            "close_app",
            re.compile(
                r"^(?:.*?\b(?:can|could|would|will) you (?:please )?)?(?:please )?"
                r"(?:close|quit|exit|kill) (?:the )?(?P<app>.+?)(?: for me)?$"
            ),
        ),
        ("web_search", re.compile(r"^(?:please )?search (?:for )?(?P<query>.+?) in (?P<browser>[a-z0-9 ]+)$")),
        ("web_search", re.compile(r"^(?:please )?search (?:for )?(?P<query>.+)$")),
        (
            "switch_app",
            re.compile(r"^(?:please )?(?:switch|focus|jump|go) (?:to )?(?:the )?(?P<app>.+)$"),
        ),
        ("open_app", re.compile(r"^(?:please )?(?:open|launch) (?P<app>.+)$")),
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
        if kind.startswith("tab:"):
            return {
                "kind": "browser_tab_control",
                "tool": "browser_tab_control",
                "arguments": {"action": kind.split(":", 1)[1]},
                "request": text,
            }
        if kind.startswith("win:"):
            return {
                "kind": "window_control",
                "tool": "window_control",
                "arguments": {"action": kind.split(":", 1)[1]},
                "request": text,
            }
        if kind.startswith("scroll:"):
            direction = groups.get("edge") or kind.split(":", 1)[1]
            return {
                "kind": "scroll_page",
                "tool": "scroll_page",
                "arguments": {"direction": direction, "amount": 5},
                "request": text,
            }
        if kind.startswith("brightness:"):
            choice = kind.split(":", 1)[1]
            if choice == "set":
                percent = _single_number(groups.get("value") or "")
                if percent is None:
                    return None
                return {
                    "kind": "set_brightness",
                    "tool": "set_brightness",
                    "arguments": {"percent": percent, "delta": 0},
                    "request": text,
                }
            return {
                "kind": "set_brightness",
                "tool": "set_brightness",
                "arguments": {"percent": -1, "delta": 10 if choice == "up" else -10},
                "request": text,
            }
        if kind.startswith("media:"):
            return {
                "kind": "media_control",
                "tool": "media_control",
                "arguments": {"action": kind.split(":", 1)[1]},
                "request": text,
            }
        if kind.startswith("system:"):
            return {
                "kind": "system_info",
                "tool": "system_info",
                "arguments": {"kind": kind.split(":", 1)[1]},
                "request": text,
            }
        if kind == "screenshot":
            return {
                "kind": "take_screenshot",
                "tool": "take_screenshot",
                "arguments": {},
                "request": text,
            }
        if kind == "close_app":
            app = app_key(_clean_target(groups["app"]))
            if not app:
                return None
            return {"kind": kind, "tool": "close_app", "arguments": {"app_name": app}, "request": text}
        if kind == "open_app":
            app = app_key(_clean_target(groups["app"]))
            if " and " in f" {app} " or " then " in f" {app} ":
                return None  # a multi-step sentence belongs to the LLM
            return {"kind": kind, "tool": "open_app", "arguments": {"app_name": app}, "request": text}
        if kind == "switch_app":
            app = app_key(_clean_target(groups["app"]))
            if not app:
                return None
            return {
                "kind": kind,
                "tool": "switch_app",
                "arguments": {"app_name": app},
                "request": text,
            }
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
            # "Open the first link for the GitHub." -> query "github".
            query = _strip_article(groups["query"])
            if not query or " and " in f" {query} " or " then " in f" {query} ":
                return None  # empty or multi-step: the LLM decides
            return {"kind": kind, "tool": "open_first_result", "arguments": {"query": query}, "request": text}
    return None


def run_fast_command(registry: ToolRegistry, command: dict[str, object]) -> str:
    """Run the matched command through the registry and build the reply in code."""
    kind = str(command.get("kind") or "")
    tool_name = command.get("tool")
    arguments = dict(command.get("arguments") or {})
    request = str(command.get("request") or "")
    if kind == "chat":
        return str(command.get("reply") or "Done.")
    if tool_name is None:
        return "Done."
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
