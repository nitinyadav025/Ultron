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
from assistant.brain.skills import memory_value
from assistant.brain.tool_replies import code_reply_for_tool
from assistant.speech.wake import normalise_spoken_command
from assistant.tools.apps import app_key
from assistant.tools.desktop_control import parse_duration_seconds
from assistant.tools.registry import ToolRegistry
from assistant.tools.volume import numbers_in_text
from assistant.tools.web import parse_search_request

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
    cleaned = re.sub(r"\s+", " ", value).strip(" .")
    cleaned = re.sub(r"\s+(?:for me|please)$", "", cleaned, flags=re.I)
    return cleaned.strip(" .")


def _strip_article(value: str) -> str:
    return re.sub(r"^(?:the|a|an) ", "", _clean_target(value)).strip()


_BROWSER_NAMES = frozenset({"firefox", "brave", "chrome"})
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_COMPOUND_SPLIT = re.compile(r"\s+(?:and|then)\s+", re.I)
_WAKE_COMMA = re.compile(r",\s*(?:hey\s+)?ultron\s+", re.I)
_LEADING_TURN = re.compile(r"^\s*(?:i'll turn|ill turn)\s+", re.I)
_FIRST_LINK = re.compile(r"(?:please )?open (?:the )?first (?:link|result)(?: please)?")
_REPEAT_REQUEST = re.compile(
    r"^(?:what did you (?:just )?say|"
    r"(?:can|could|would) you (?:please )?repeat(?: that| it)?|"
    r"repeat (?:that|it)|say (?:that|it) again)\b"
)
_WRITE_IT = frozenset(
    {"write it", "yes write it", "write that", "type it", "yes type it", "write this"}
)
_EMAIL_COMMANDS = frozenset(
    {
        "enter the email",
        "enter email",
        "type the email",
        "type email",
        "enter my email",
        "type my email",
        "write the email",
        "write my email",
    }
)
_DOCUMENT_TARGET = re.compile(
    r"\b(?:in|inside|into)\s+(?:this|the|a|my)?\s*(?:new\s+)?"
    r"(?:document|doc|tab|window|page|editor|file)\b",
    re.I,
)
_TIMER_COMMAND = re.compile(
    r"^(?:please )?(?:(?:set|start) (?:a )?timer (?:for )?|timer (?:for )?)"
    r"(?P<duration>.+)$"
)


def _prepare_text(text: str) -> str:
    """Drop the "I'll turn" wake miss and turn ", Ultron" into a command joiner."""
    cleaned = _LEADING_TURN.sub("", text or "")
    cleaned = _WAKE_COMMA.sub(" and ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]


def _compound_parts(text: str) -> list[str]:
    return [part.strip(" ,") for part in _COMPOUND_SPLIT.split(text or "") if part.strip(" ,")]


def _bad_app_target(app: str) -> bool:
    """True when the captured "app" is really two commands or another sentence."""
    padded = f" {app} "
    return any(
        marker in padded
        for marker in (
            " and ",
            " then ",
            " ultron ",
            " i'll turn ",
            " i ll turn ",
            " ill turn ",
            " first link ",
            " first result ",
        )
    )


_REQUEST_PREFIX = re.compile(
    r"^(?:(?:yes|yeah|ok|okay)\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:tell\s+me\s+)?|"
    r"(?:i\s+want\s+you\s+to|i'd\s+like\s+you\s+to|tell\s+me|please)\s+)"
)


def _strip_request_prefixes(cleaned: str) -> str:
    # "Okay, close the tab" has no request verb after "okay", so strip the ack first.
    cleaned = re.sub(r"^(?:yes|yeah|ok|okay)[, ]+", "", cleaned).strip()
    for _ in range(3):
        stripped = _REQUEST_PREFIX.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


def _match_single(text: str) -> dict[str, object] | None:
    """Return {"kind", "tool", "arguments", "request"} for one simple sentence."""
    raw = _prepare_text((text or "").strip())
    repaired = normalise_spoken_command(raw)
    spoken = _normalise(repaired)

    # Repeat requests are answered from SESSION.last_reply, even when the previous turn
    # was a fast-path action or a learned skill (the split-brain fix).
    if _REPEAT_REQUEST.match(spoken):
        return _chat(
            spoken,
            SESSION.last_reply or "I haven't said anything yet.",
            text,
        )

    # "write it" / "yes write it" / "right it": paste the previous answer immediately.
    if spoken in _WRITE_IT:
        if not SESSION.last_reply:
            return _chat(spoken, "What should I write?", text)
        return {
            "kind": "type_text",
            "tool": "type_text",
            "arguments": {"text": SESSION.last_reply},
            "request": text,
        }

    # Stored facts: "enter the email" pastes user_memory["email"].
    if spoken in _EMAIL_COMMANDS:
        email = memory_value("email")
        if not email:
            return _chat(spoken, "I don't have your email saved yet.", text)
        return {
            "kind": "type_text",
            "tool": "type_text",
            "arguments": {"text": email},
            "request": text,
        }

    typed = re.match(
        r"(?i)^(?:(?:can|could|would|will) you )?(?:please )?"
        r"(?:type|write|enter text) (.+)$",
        repaired,
    )
    if typed:
        text_to_type = typed.group(1).strip()
        # "write what is an AI agent in this new document" is content generation, not a
        # literal dictation: let the LLM write the paragraph and call type_text.
        if _DOCUMENT_TARGET.search(text_to_type):
            return None
        terminal = bool(re.search(r"\bin (?:the )?terminal$", text_to_type, re.I))
        if terminal:
            text_to_type = re.sub(r"\s+in (?:the )?terminal$", "", text_to_type, flags=re.I).strip()
        return {
            "kind": "type_text",
            "tool": "type_text",
            "arguments": {"text": text_to_type},
            "terminal": terminal,
            "request": text,
        }

    cleaned = _strip_request_prefixes(spoken)
    if not cleaned:
        return None

    # "search f1 in youtube", "search on youtube for f1", "search youtube".
    site_request = parse_search_request(repaired)
    if site_request is not None:
        query, site = site_request
        arguments: dict[str, object] = {"query": query, "site": site}
        if SESSION.last_browser:
            arguments["browser"] = SESSION.last_browser
        return {
            "kind": "web_search",
            "tool": "web_search",
            "arguments": arguments,
            "request": text,
        }

    # "set a timer for 5 seconds", "timer for 1 minute 30 seconds".
    timer_request = _TIMER_COMMAND.match(cleaned)
    if timer_request:
        seconds = parse_duration_seconds(timer_request.group("duration"))
        if seconds is not None:
            return {
                "kind": "set_timer",
                "tool": "set_timer",
                "arguments": {"seconds": seconds},
                "request": text,
            }

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
    } or _FIRST_LINK.fullmatch(cleaned):
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
        ("tab:new_tab", re.compile(r"^(?:please )?(?:go to|open|switch to) (?:the )?new tab$")),
        ("tab:new_tab", re.compile(r"^(?:please )?open (?:the )?tab$")),
        (
            "tab:new_tab_in",
            re.compile(r"^(?:please )?open (?:a )?new tab in (?P<browser>[a-z0-9 ]+)$"),
        ),
        ("tab:new_tab_in", re.compile(r"^(?:please )?new tab in (?P<browser>[a-z0-9 ]+)$")),
        (
            "tab:close_tab",
            re.compile(r"^(?:please )?(?:close|shut) (?:this |the )?tab(?: .*)?$"),
        ),
        (
            "tab:next_tab",
            re.compile(r"^(?:please )?(?:switch|jump) to (?:the )?next tab$"),
        ),
        (
            "tab:prev_tab",
            re.compile(r"^(?:please )?(?:switch|jump) to (?:the )?previous tab$"),
        ),
        ("tab:next_tab", re.compile(r"^(?:please )?(?:next|switch) tab$")),
        ("tab:prev_tab", re.compile(r"^(?:please )?(?:previous|prev) tab$")),
        (
            "tab:reopen_tab",
            re.compile(r"^(?:please )?(?:reopen|restore) (?:the )?(?:last )?tab$"),
        ),
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
        # One allow-listed key press.
        (
            "press_key",
            re.compile(r"^(?:please )?press (?:the )?(?P<key>[a-z0-9 ]+?)(?: key)?$"),
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
        (
            "brightness:up",
            re.compile(r"^(?:please )?(?:increase(?: the)? brightness|brightness up)$"),
        ),
        (
            "brightness:down",
            re.compile(r"^(?:please )?(?:decrease(?: the)? brightness|brightness down)$"),
        ),
        (
            "brightness:set",
            re.compile(r"^(?:please )?set brightness to (?P<value>.+)$"),
        ),
        ("media:play_pause", re.compile(r"^(?:please )?(?:play|pause|resume)(?: the)? music$")),
        ("media:play_pause", re.compile(r"^(?:please )?(?:play|pause|resume)$")),
        ("media:next", re.compile(r"^(?:please )?next (?:song|track)$")),
        ("media:previous", re.compile(r"^(?:please )?(?:previous|prev) (?:song|track)$")),
        # System information and screenshot.
        (
            "system:battery",
            re.compile(
                r"^(?:please )?(?:what is my battery|my battery|the battery|battery status|"
                r"tell me my battery|how is my battery|battery level)$"
            ),
        ),
        (
            "system:time",
            re.compile(
                r"^(?:please )?(?:what time is it|what is the time|tell me the time|"
                r"the time|current time|the current time|what is the current time)$"
            ),
        ),
        (
            "system:date",
            re.compile(
                r"^(?:please )?(?:what is today s date|what is today s day|what date is it|"
                r"tell me the current date|tell me today s date|what is the current date|"
                r"current date|the current date|the date|what is today s date)$"
            ),
        ),
        ("system:status", re.compile(r"^(?:please )?(?:system status|system info)$")),
        (
            "screenshot",
            re.compile(
                r"^(?:please )?(?:take (?:a )?screenshots?|capture (?:the )?screen(?:shot)?s?|"
                r"screenshots?)$"
            ),
        ),
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
        (
            "open_first_video",
            re.compile(
                r"^(?:please )?(?:play|open|click) (?:the )?first video"
                r"(?: (?:for|of|on) (?P<query>.+))?$"
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
        (
            "volume_up",
            re.compile(
                r"^(?:please )?(?:turn(?: the)? volume up|volume up|increase(?: the)? volume|louder)$"
            ),
        ),
        (
            "volume_down",
            re.compile(
                r"^(?:please )?(?:turn(?: the)? volume down|volume down|decrease(?: the)? volume|quieter)$"
            ),
        ),
        ("set_volume", re.compile(r"^(?:please )?(?:turn|set|change) (?:the )?volume to (?P<value>.+)$")),
        ("list_files", re.compile(r"^(?:please )?(?:list|show) (?:the )?files (?:in|from) (?P<folder>.+)$")),
        (
            "list_files",
            re.compile(
                r"^(?:please )?(?:list|show) (?:my |the )?"
                r"(?P<folder>downloads|documents|desktop|pictures|home)(?: folder| directory)?$"
            ),
        ),
    ]

    for kind, pattern in patterns:
        match = pattern.match(cleaned)
        if not match:
            continue
        groups = match.groupdict()
        if kind == "tab:new_tab_in":
            browser = app_key(_clean_target(groups.get("browser") or ""))
            if browser not in _BROWSER_NAMES:
                return None
            return {
                "kind": "compound",
                "tool": None,
                "commands": [
                    {
                        "kind": "switch_app",
                        "tool": "switch_app",
                        "arguments": {"app_name": browser},
                        "request": text,
                    },
                    {
                        "kind": "browser_tab_control",
                        "tool": "browser_tab_control",
                        "arguments": {"action": "new_tab"},
                        "request": text,
                    },
                ],
                "request": text,
            }
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
        if kind == "press_key":
            return {
                "kind": kind,
                "tool": "press_key",
                "arguments": {"key": _clean_target(groups.get("key") or "")},
                "request": text,
            }
        if kind.startswith("scroll:"):
            direction = groups.get("edge") or kind.split(":", 1)[1]
            return {
                "kind": "scroll_page",
                "tool": "scroll_page",
                "arguments": {"direction": direction, "amount": 1},
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
            target = _clean_target(groups["app"])
            app = app_key(target)
            if not app or _bad_app_target(target):
                return None
            return {"kind": kind, "tool": "close_app", "arguments": {"app_name": app}, "request": text}
        if kind == "open_app":
            target = _clean_target(groups["app"])
            app = app_key(target)
            if not app or _bad_app_target(target):
                return None  # a multi-step sentence belongs to the compound splitter
            return {"kind": kind, "tool": "open_app", "arguments": {"app_name": app}, "request": text}
        if kind == "switch_app":
            target = _clean_target(groups["app"])
            app = app_key(target)
            if not app or _bad_app_target(target):
                return None
            return {
                "kind": kind,
                "tool": "switch_app",
                "arguments": {"app_name": app},
                "request": text,
            }
        if kind == "volume_up":
            return {"kind": kind, "tool": "volume_up", "arguments": {}, "request": text}
        if kind == "volume_down":
            return {"kind": kind, "tool": "volume_down", "arguments": {}, "request": text}
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
            if not folder or _bad_app_target(folder):
                return None
            return {"kind": kind, "tool": "list_files", "arguments": {"directory": folder}, "request": text}
        if kind == "open_first_video":
            query = _clean_target(groups.get("query") or SESSION.last_query)
            if not query:
                return _chat(cleaned, "What should I play on YouTube?", text)
            return {
                "kind": "open_first_result",
                "tool": "open_first_result",
                "arguments": {"query": query, "site": "youtube", "first_video": True},
                "request": text,
            }
        if kind == "web_search":
            query = _clean_target(groups["query"])
            raw_target = _clean_target(groups.get("browser") or "")
            if not query:
                return None
            if raw_target:
                browser = app_key(raw_target)
                if browser in _BROWSER_NAMES:
                    return {
                        "kind": kind,
                        "tool": "web_search",
                        "arguments": {"query": query, "browser": browser},
                        "request": text,
                    }
                # "search f1 in youtube" searches the site, not a browser named youtube.
                from assistant.tools.web import normalise_site

                site = normalise_site(raw_target)
                if not site:
                    # "open a new tab and search for github" is two actions, not a
                    # browser/site named "a new tab". Let the compound splitter handle it.
                    return None
                site_args: dict[str, object] = {"query": query, "site": site}
                if SESSION.last_browser:
                    site_args["browser"] = SESSION.last_browser
                return {
                    "kind": kind,
                    "tool": "web_search",
                    "arguments": site_args,
                    "request": text,
                }
            return {
                "kind": kind,
                "tool": "web_search",
                "arguments": {"query": query, "browser": None},
                "request": text,
            }
        if kind == "open_first_result":
            # "Open the first link for the GitHub." -> query "github".
            query = _strip_article(groups["query"])
            if not query or _bad_app_target(query):
                return None  # empty or multi-step: the LLM decides
            return {"kind": kind, "tool": "open_first_result", "arguments": {"query": query}, "request": text}
    return None


def match_fast_command(text: str) -> dict[str, object] | None:
    """Match one sentence, a multi-sentence transcript, or two commands joined by and/then.

    Priority: a whole-sentence fast path first (so "open firefox and search for github" is
    one web_search), then each sentence of a merged transcript, then a compound split.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    prepared = _prepare_text(raw)
    single = _match_single(prepared)
    if single is not None:
        return single
    for sentence in _sentences(prepared):
        if sentence == prepared:
            continue
        single = _match_single(sentence)
        if single is not None:
            return single
    parts = _compound_parts(prepared)
    if len(parts) >= 2:
        commands: list[dict[str, object]] = []
        for part in parts:
            command = _match_single(part)
            if command is None or command.get("kind") == "chat":
                return None
            commands.append(command)
        return {"kind": "compound", "tool": None, "commands": commands, "request": text}
    return None


def run_fast_command(registry: ToolRegistry, command: dict[str, object]) -> str:
    """Run the matched command through the registry and build the reply in code."""
    kind = str(command.get("kind") or "")
    tool_name = command.get("tool")
    arguments = dict(command.get("arguments") or {})
    request = str(command.get("request") or "")
    if kind == "chat":
        return str(command.get("reply") or "Done.")
    if kind == "compound":
        replies: list[str] = []
        for sub in command.get("commands") or []:
            reply = run_fast_command(registry, dict(sub))  # type: ignore[arg-type]
            if reply and reply not in replies:
                replies.append(reply)
        return " ".join(replies) or "Done."
    if tool_name is None:
        return "Done."
    if command.get("terminal"):
        try:
            from assistant.tools.desktop_control import note_explicit_terminal

            note_explicit_terminal()
        except Exception:
            pass
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
    reply = run_fast_command(registry, command)
    if reply:
        SESSION.last_reply = reply
    return reply
