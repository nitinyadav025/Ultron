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


def _raw_summary(result: dict[str, Any]) -> dict[str, Any]:
    raw = result.get("raw_summary")
    return raw if isinstance(raw, dict) else result


def _result_ok(result: dict[str, Any]) -> bool:
    """The registry wraps a tool call as ok=True; the tool's own ok must also be true."""
    if not isinstance(result, dict) or not result.get("ok", False):
        return False
    raw = result.get("raw_summary")
    if isinstance(raw, dict) and "ok" in raw:
        return bool(raw.get("ok"))
    return True


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
    if not _result_ok(result):
        return FOLDER_NOT_FOUND_REPLY
    raw = _raw_summary(result)
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


def mute_audio_reply(result: dict[str, Any]) -> str | None:
    """Speak exactly what mute_audio did: muted, unmuted, or a failure."""
    if not _result_ok(result):
        return "I couldn't change the audio."
    raw = _raw_summary(result)
    muted = raw.get("muted")
    if muted is True:
        return "Audio is now muted."
    if muted is False:
        return "Audio is now unmuted."
    return None


def volume_reply(result: dict[str, Any]) -> str | None:
    """Speak the absolute volume the tool actually set."""
    if not _result_ok(result):
        return "I couldn't set the volume."
    raw = _raw_summary(result)
    value = raw.get("requested_percent", raw.get("volume_percent"))
    try:
        percent = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return f"Volume is now set to {percent} percent."


def _app_display(app: str) -> str:
    return app.title() if app.islower() else app


def open_app_reply(result: dict[str, Any], requested: str = "") -> str | None:
    """Speak the app that was actually launched, or say honestly that it cannot."""
    raw = _raw_summary(result)
    app = str(raw.get("app_name") or requested or "").strip()
    if raw.get("unknown"):
        return f"I couldn't find an app named {app or 'that'}."
    if raw.get("web_app"):
        return str(raw.get("reply") or f"Opening {app or 'that'} in the browser.")
    if not _result_ok(result):
        return "I couldn't open that app."
    if not app:
        return "Done."
    return f"I've opened {_app_display(app)} for you."


def close_app_reply(result: dict[str, Any]) -> str | None:
    """Speak exactly what close_app did: closed, not running, or unknown."""
    raw = _raw_summary(result)
    app = str(raw.get("app_name") or "").strip()
    display = _app_display(app) if app else "That app"
    if raw.get("unknown"):
        return f"I couldn't find an app named {app or 'that'}."
    if raw.get("tab_closed"):
        if not _result_ok(result):
            return str(raw.get("error") or "I couldn't close that tab.")
        if app.lower() in ("", "that", "tab", "this", "current"):
            return "I've closed that tab for you."
        return f"I've closed the {_app_display(app)} tab."
    if not _result_ok(result):
        return f"I couldn't close {display}."
    if raw.get("closed") is True:
        return f"I've closed {display} for you."
    if raw.get("running") is False:
        return f"{display} isn't running right now."
    return None


def switch_app_reply(result: dict[str, Any]) -> str | None:
    """Speak whether Ultron focused the existing window or had to open the app."""
    raw = _raw_summary(result)
    app = str(raw.get("app_name") or "").strip()
    display = _app_display(app) if app else "That app"
    if raw.get("unknown"):
        return f"I couldn't find an app named {app or 'that'}."
    if not _result_ok(result):
        return f"I couldn't switch to {display}."
    if raw.get("opened") and not raw.get("running"):
        return f"{display} wasn't running, so I've opened it for you."
    return f"I've switched to {display}."


def type_text_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't type that.")
    return str(raw.get("reply") or "I've typed that into the active window.")


def press_key_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't press that key.")
    return str(raw.get("reply") or "I've pressed that key.")


def scroll_page_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't scroll.")
    return {
        "down": "I've scrolled the page down.",
        "up": "I've scrolled the page up.",
        "page_down": "I've scrolled down one page.",
        "page_up": "I've scrolled up one page.",
        "top": "I've scrolled to the top.",
        "bottom": "I've scrolled to the bottom.",
    }.get(str(raw.get("direction") or ""), "I've scrolled the page.")


def browser_tab_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't control the tab.")
    return {
        "new_tab": "I've opened a new tab.",
        "close_tab": "I've closed that tab.",
        "next_tab": "I've switched to the next tab.",
        "prev_tab": "I've switched to the previous tab.",
        "reopen_tab": "I've reopened the last tab.",
        "refresh": "I've refreshed the page.",
        "back": "I've gone back.",
        "forward": "I've gone forward.",
    }.get(str(raw.get("action") or ""), "Done.")


def window_control_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't control the window.")
    return {
        "next_window": "I've switched windows.",
        "maximize": "I've maximized the window.",
        "minimize": "I've minimized the window.",
        "fullscreen": "I've toggled fullscreen.",
        "workspace_next": "I've moved to the next workspace.",
        "workspace_prev": "I've moved to the previous workspace.",
        "overview": "I've opened the overview.",
    }.get(str(raw.get("action") or ""), "Done.")


def brightness_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't change the brightness.")
    percent = raw.get("percent")
    return (
        f"I've set the brightness to {percent} percent."
        if percent is not None
        else "I've changed the brightness."
    )


def media_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't control the media.")
    return {
        "play_pause": "I've toggled play or pause.",
        "play": "I've started the music.",
        "pause": "I've paused the music.",
        "next": "I've skipped to the next song.",
        "previous": "I've gone back one song.",
    }.get(str(raw.get("action") or ""), "Done.")


def system_info_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't read the system information.")
    return str(raw.get("reply") or "Done.")


def screenshot_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't take a screenshot.")
    path = str(raw.get("path") or "").strip()
    return f"I've saved the screenshot to {path}." if path else "I've taken the screenshot."


_SITE_DISPLAY = {
    "youtube": "YouTube",
    "github": "GitHub",
    "reddit": "Reddit",
    "wikipedia": "Wikipedia",
    "google": "Google",
    "amazon": "Amazon",
    "spotify": "Spotify",
    "gmail": "Gmail",
}


def web_search_reply(result: dict[str, Any]) -> str | None:
    """Speak the query and the browser that was used."""
    raw = _raw_summary(result)
    if not _result_ok(result):
        return "I couldn't open the search."
    query = str(raw.get("query") or "").strip()
    site = str(raw.get("site") or "").strip()
    browser = str(raw.get("browser") or "").strip()
    if site and not query:
        return str(raw.get("reply") or f"Opening {_SITE_DISPLAY.get(site, site.title())}.")
    if site:
        return f"Searching {_SITE_DISPLAY.get(site, site.title())} for {query} right now."
    if browser:
        return f"Searching the web for {query} in {browser.title()} right now."
    return f"Searching the web for {query} right now."


def set_timer_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't set that timer.")
    return str(raw.get("reply") or "I've set the timer.")


def open_first_result_reply(result: dict[str, Any]) -> str | None:
    """Speak the domain that was opened, or the honest fallback sentence."""
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("reply") or "I couldn't open the search.")
    if raw.get("reply"):
        return str(raw["reply"])
    query = str(raw.get("query") or "").strip()
    return f"I've opened the first result for {query}."


def click_element_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't click that.")
    return str(raw.get("reply") or f"I've clicked {raw.get('target') or 'that'}.")


def read_screen_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("reply") or raw.get("error") or "I can't read the screen right now.")
    return str(raw.get("reply") or "I can't see anything on the screen right now.")


def focus_and_type_reply(result: dict[str, Any]) -> str | None:
    raw = _raw_summary(result)
    if not _result_ok(result):
        return str(raw.get("error") or "I couldn't type into that field.")
    return str(raw.get("reply") or "I've typed that into the field.")


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
    if tool_name == "mute_audio":
        return mute_audio_reply(result)
    if tool_name in ("set_volume", "volume_up", "volume_down"):
        return volume_reply(result)
    if tool_name == "open_app":
        return open_app_reply(result, str((result.get("raw_summary") or {}).get("app_name", "")))
    if tool_name == "close_app":
        return close_app_reply(result)
    if tool_name == "switch_app":
        return switch_app_reply(result)
    if tool_name == "type_text":
        return type_text_reply(result)
    if tool_name == "press_key":
        return press_key_reply(result)
    if tool_name == "scroll_page":
        return scroll_page_reply(result)
    if tool_name == "browser_tab_control":
        return browser_tab_reply(result)
    if tool_name == "window_control":
        return window_control_reply(result)
    if tool_name == "set_brightness":
        return brightness_reply(result)
    if tool_name == "media_control":
        return media_reply(result)
    if tool_name == "system_info":
        return system_info_reply(result)
    if tool_name == "take_screenshot":
        return screenshot_reply(result)
    if tool_name == "set_timer":
        return set_timer_reply(result)
    if tool_name == "web_search":
        return web_search_reply(result)
    if tool_name == "open_first_result":
        return open_first_result_reply(result)
    if tool_name == "click_element":
        return click_element_reply(result)
    if tool_name == "read_screen":
        return read_screen_reply(result)
    if tool_name == "focus_and_type":
        return focus_and_type_reply(result)
    return None


def fallback_reply(tool_name: str) -> str:
    """Short safe sentence when the model only produced a lead-in for ``tool_name``."""
    return {
        "list_files": "I listed the folder.",
        "read_file": "I read the file.",
        "open_app": "Done.",
    }.get(tool_name, "Done.")
