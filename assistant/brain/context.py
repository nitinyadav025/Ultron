"""Small session memory shared by tools and the fast path.

It stores what a follow-up needs (last app, browser, query, recent turns) plus a live
screen snapshot read from ``wmctrl -l``. Nothing is written to disk, so "close it" and
"open the first link" work only inside the current session.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("ultron.context")

BROWSERS = frozenset({"firefox", "brave", "chrome"})

# Window-title keywords -> site key. Order matters: the first match wins.
SITE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("youtube", "youtube"),
    ("github", "github"),
    ("instagram", "instagram"),
    ("discord", "discord"),
    ("visual studio code", "vscode"),
    ("vs code", "vscode"),
    ("reddit", "reddit"),
    ("wikipedia", "wikipedia"),
    ("whatsapp", "whatsapp"),
    ("netflix", "netflix"),
    ("spotify", "spotify"),
    ("chatgpt", "chatgpt"),
    ("gmail", "gmail"),
    ("amazon", "amazon"),
    ("linkedin", "linkedin"),
    ("twitter", "twitter"),
    (" x ", "twitter"),
    ("google", "google"),
)

# Sites that accept a search URL and can be searched "in place".
SEARCHABLE_SITES = frozenset(
    {"youtube", "github", "reddit", "wikipedia", "google", "amazon", "spotify", "gmail"}
)

_WINDOW_RUNNER: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
_WMCTRL_TIMEOUT_S = 0.2


@dataclass
class SessionContext:
    last_app: str = ""
    last_browser: str = ""
    last_query: str = ""
    last_site: str = ""
    active_site: str = ""
    open_windows: list[str] = field(default_factory=list)
    turns: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=5))

    def reset(self) -> None:
        self.last_app = ""
        self.last_browser = ""
        self.last_query = ""
        self.last_site = ""
        self.active_site = ""
        self.open_windows.clear()
        self.turns.clear()

    def note_app(self, app_name: str, *, browser: bool | None = None) -> None:
        cleaned = (app_name or "").strip().lower()
        if not cleaned:
            return
        self.last_app = cleaned
        if browser is None:
            browser = cleaned in BROWSERS
        if browser:
            self.last_browser = cleaned

    def note_closed(self, app_name: str) -> None:
        cleaned = (app_name or "").strip().lower()
        if cleaned and self.last_app == cleaned:
            self.last_app = ""

    def note_search(self, query: str, browser: str | None = None) -> None:
        self.last_query = (query or "").strip()
        if browser:
            self.last_browser = (browser or "").strip().lower()

    def note_screen(self, windows: list[str]) -> None:
        """Update browser/site from real window titles (newest window wins)."""
        self.open_windows = list(windows)[:20]
        browser = ""
        site = ""
        for title in reversed(self.open_windows):
            lowered = title.lower()
            if not browser:
                if "mozilla firefox" in lowered or lowered.endswith("firefox"):
                    browser = "firefox"
                elif "brave" in lowered:
                    browser = "brave"
                elif "google chrome" in lowered or "chromium" in lowered:
                    browser = "chrome"
            if not site:
                for keyword, key in SITE_KEYWORDS:
                    if keyword in lowered:
                        site = key
                        break
        if browser:
            self.last_browser = browser
        if site:
            self.active_site = site
            self.last_site = site

    def note_turn(self, user: str, assistant: str) -> None:
        self.turns.append(((user or "").strip(), (assistant or "").strip()))

    def recent_turns(self, limit: int = 4) -> list[tuple[str, str]]:
        return list(self.turns)[-max(1, int(limit)):]


SESSION = SessionContext()


def parse_window_titles(output: str) -> list[str]:
    """Parse ``wmctrl -l`` lines: "0xID desktop host title" -> titles."""
    titles: list[str] = []
    for line in (output or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            title = parts[3].strip()
            if title:
                titles.append(title)
    return titles


def read_window_titles(runner: Callable[..., Any] | None = None) -> list[str]:
    """Run ``wmctrl -l`` with a short timeout; never raise into a command.

    Automated tests never read the real desktop: pass a fake ``runner`` to exercise the
    parser. Production always uses the real window list.
    """
    if runner is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    run = runner or _WINDOW_RUNNER
    try:
        result = run(
            ["wmctrl", "-l"], capture_output=True, text=True, timeout=_WMCTRL_TIMEOUT_S
        )
    except Exception as exc:  # wmctrl missing, timeout, non-zero exit
        logger.debug("Screen context unavailable: %s", type(exc).__name__)
        return []
    if getattr(result, "returncode", 1) != 0:
        return []
    return parse_window_titles(getattr(result, "stdout", "") or "")


def get_screen_context(runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Read the live windows and update SESSION; return a compact snapshot."""
    windows = read_window_titles(runner)
    if windows:
        SESSION.note_screen(windows)
    return {
        "windows": list(SESSION.open_windows),
        "browser": SESSION.last_browser,
        "site": SESSION.active_site,
    }


def screen_line(context: dict[str, Any] | None = None) -> str:
    """One compact line injected into every LLM call."""
    data = context or get_screen_context()
    windows = [re.sub(r"\s+", " ", title)[:45] for title in data.get("windows", [])[:4]]
    return (
        f"Screen: browser={data.get('browser') or 'unknown'}, "
        f"site={data.get('site') or 'unknown'}, "
        f"windows=[{'; '.join(windows)}]"
    )


def active_search_site() -> str:
    """The site currently on screen when it supports an in-site search, else ''."""
    site = (SESSION.active_site or "").strip().lower()
    return site if site in SEARCHABLE_SITES else ""
