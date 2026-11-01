"""Wake-word matching for hands-free mode.

The user says "Ultron, <command>". The first one or two words are compared fuzzily
(difflib ratio >= 0.75) with the wake word and its aliases.

Where the built-in aliases come from (evidence, 2026-11-01): the project's Piper voice
synthesized ten WAV files (no playback) and the production Whisper ears read them back.
Whisper wrote `Ultron, mute.` for four phrases but also `Ultra-open Firefox`,
`Ultra on Search Kit Home` and `Ultra, open Firefox.` for the others. "ultra" is therefore
a measured alias. **Piper's voice is not the user's voice**, so every alias is UNVERIFIED
until his own live test; `WAKE_ALIASES` exists to extend the list without code changes.
"""

from __future__ import annotations

import difflib
import re

DEFAULT_ALIASES = ("ultra", "hey ultron")
RATIO = 0.75


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _alias_list(wake_word: str, aliases: str | tuple[str, ...] | list[str] = "") -> list[str]:
    if isinstance(aliases, str):
        extra = [item.strip().lower() for item in aliases.split(",") if item.strip()]
    else:
        extra = [str(item).strip().lower() for item in aliases if str(item).strip()]
    words = [wake_word.strip().lower()] + list(DEFAULT_ALIASES) + extra
    return [w for w in dict.fromkeys(words) if w]


def split_wake_word(
    text: str, wake_word: str = "ultron", aliases: str | tuple[str, ...] | list[str] = ""
) -> tuple[bool, str]:
    """Return (wake_word_found, command_text_after_it)."""
    tokens = _tokens(text)
    if not tokens:
        return False, ""
    targets = _alias_list(wake_word, aliases)
    best: tuple[float, int] | None = None  # (ratio, words_consumed)

    # The shortest match wins a tie: Whisper can join "Ultron open" into "Ultra-open", and
    # taking only "Ultra" then leaves the real command word "open" in place.
    for count in (1, 2):
        if len(tokens) < count:
            continue
        candidate = " ".join(tokens[:count])
        for target in targets:
            if len(target.split()) != count:
                continue
            ratio = difflib.SequenceMatcher(None, candidate, target).ratio()
            if ratio < RATIO:
                continue
            if best is None or ratio > best[0] + 1e-9:
                best = (ratio, count)
    if best is None:
        return False, ""
    consumed = best[1]
    return True, " ".join(tokens[consumed:]).strip()


DANGLING_WORDS = {
    "and",
    "then",
    "the",
    "a",
    "to",
    "for",
    "with",
    "in",
    "of",
    "on",
    "my",
    "or",
    "but",
    "also",
}


def sentence_is_complete(text: str) -> bool:
    """False when the sentence stops after a joining word or a comma ("open firefox and")."""
    cleaned = (text or "").strip()
    if cleaned.endswith(","):
        return False
    tokens = _tokens(cleaned)
    if not tokens:
        return True
    return tokens[-1] not in DANGLING_WORDS


def first_word(text: str) -> str:
    tokens = _tokens(text)
    return tokens[0] if tokens else ""
