"""Small session memory shared by tools and the fast path.

It deliberately stores only what a follow-up needs: the last app, the last browser, the
last search query and the last five conversation turns. Nothing is written to disk, so
"close it" and "open the first link" work only inside the current session.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

BROWSERS = frozenset({"firefox", "brave", "chrome"})


@dataclass
class SessionContext:
    last_app: str = ""
    last_browser: str = ""
    last_query: str = ""
    turns: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=5))

    def reset(self) -> None:
        self.last_app = ""
        self.last_browser = ""
        self.last_query = ""
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

    def note_turn(self, user: str, assistant: str) -> None:
        self.turns.append(((user or "").strip(), (assistant or "").strip()))

    def recent_turns(self, limit: int = 4) -> list[tuple[str, str]]:
        return list(self.turns)[-max(1, int(limit)):]


SESSION = SessionContext()
