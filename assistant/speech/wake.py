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
import json
import re
from dataclasses import dataclass
from pathlib import Path

# Safe spellings: a fuzzy match is trusted even when the rest is ordinary talk.
SAFE_DEFAULT_ALIASES = ("outron", "ultra", "hey ultron")
# Phonetic variants measured in the user's own wake-train clips (Nov 1, 18:01). These
# are ordinary English words, so a match is trusted only when the remainder is empty or
# a known fast-path command (handled by ``learned=True`` in WakeMatch).
RISKY_ALIASES = (
    "i'm drawn",
    "i'll draw",
    "i'll drawn",
    "i drawn",
    "i've drawn",
    "i'll try on",
    "are you drawn",
    "hey all drawn",
    "hey all right",
)
DEFAULT_ALIASES = SAFE_DEFAULT_ALIASES + RISKY_ALIASES
RATIO = 0.75
# Whisper inserts small connector words after a misheard wake word:
# "I'll draw an open Firefox." must become "open firefox".
FILLER_WORDS = {"an", "and", "to", "uh", "um", "please"}
# Common app mishearings in the saved clips (B3 wake-train table).
_SPOKEN_COMMAND_FIXES = (
    (re.compile(r"\b(open|launch) (?:5ox|5x|file box|fire box|fire fox)\b", re.I), r"\1 firefox"),
)


def normalise_spoken_command(text: str) -> str:
    """Repair known Whisper app mishearings and collapse spaces."""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    for pattern, replacement in _SPOKEN_COMMAND_FIXES:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


@dataclass(frozen=True)
class WakeMatch:
    found: bool
    command: str = ""
    ratio: float = 0.0
    target: str = ""
    learned: bool = False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _alias_list(wake_word: str, aliases: str | tuple[str, ...] | list[str] = "") -> list[str]:
    if isinstance(aliases, str):
        extra = [item.strip().lower() for item in aliases.split(",") if item.strip()]
    else:
        extra = [str(item).strip().lower() for item in aliases if str(item).strip()]
    words = [wake_word.strip().lower()] + list(DEFAULT_ALIASES) + extra
    return [w for w in dict.fromkeys(words) if w]


def load_learned_aliases(path: Path | str | None) -> list[str]:
    """Read workspace/wake_aliases.json (written by `main.py wake-train`).

    Accepts {"aliases": {"ultron": 12, ...}} or {"aliases": ["ultron", ...]}.
    A missing or broken file means "no learned aliases", never an error.
    """
    if not path:
        return []
    target = Path(path).expanduser()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("aliases", data) if isinstance(data, dict) else data
    names: list[str] = []
    if isinstance(raw, dict):
        names = [str(key) for key, count in raw.items() if count]
    elif isinstance(raw, list):
        names = [str(item.get("text") if isinstance(item, dict) else item) for item in raw]
    cleaned = [" ".join(str(name).lower().split()) for name in names]
    # Wake-train also records ordinary sentence starters ("i", "hey", "what"). Those
    # would make any "I ..." sentence look like a wake word; keep only useful spellings.
    common = {"i", "hey", "what", "the", "and", "a", "to", "you", "it", "is", "so"}
    return [
        name
        for name in dict.fromkeys(cleaned)
        if name and (len(name.split()) > 1 or (len(name) >= 5 and name not in common))
    ]


def strip_wake_prefixes(
    text: str,
    wake_word: str = "ultron",
    aliases: str | tuple[str, ...] | list[str] = "",
    learned_aliases: list[str] | tuple[str, ...] = (),
    max_rounds: int = 6,
) -> str:
    """Remove repeated/nested wake spellings from the command text.

    "Ultron, I'll draw an open Firefox" -> "open firefox"; "Ultron, Ultron" -> "".
    Only text that starts with a known wake spelling is consumed, so a real command is
    returned unchanged.
    """
    remaining = (text or "").strip()
    for _ in range(max(1, max_rounds)):
        match = wake_match(remaining, wake_word, aliases, learned_aliases)
        if not match.found:
            return remaining
        if not match.command or match.command == remaining:
            return match.command
        remaining = match.command
    return remaining


def aliases_for(
    wake_word: str = "ultron",
    aliases: str | tuple[str, ...] | list[str] = "",
    learned: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Every wake spelling currently tried (wake word, built-ins, configured, learned)."""
    return _alias_list(wake_word, aliases) + [name for name in learned if name not in _alias_list(wake_word, aliases)]


def wake_match(
    text: str,
    wake_word: str = "ultron",
    aliases: str | tuple[str, ...] | list[str] = "",
    learned_aliases: list[str] | tuple[str, ...] = (),
) -> WakeMatch:
    """Return the best wake-word match, including its score and target (for debug output).

    Built-in/configured spellings may be one or two words; learned spellings up to three.
    On a tie the shorter match wins so a real command word is not swallowed.
    """
    tokens = _tokens(text)
    if not tokens:
        return WakeMatch(False)
    builtin = _alias_list(wake_word, aliases)
    learned = [" ".join(str(item).lower().split()) for item in learned_aliases if str(item).strip()]
    restricted = set(RISKY_ALIASES)
    targets: list[tuple[str, bool]] = [(name, name in restricted) for name in builtin]
    targets += [(name, True) for name in learned if name not in builtin]
    best: tuple[float, int, str, bool] | None = None  # (ratio, words_consumed, target, learned)

    for count in (1, 2, 3):
        if len(tokens) < count:
            continue
        candidate = " ".join(tokens[:count])
        for target, is_learned in targets:
            if len(target.split()) != count:
                continue
            if is_learned and count > 3:
                continue
            ratio = difflib.SequenceMatcher(None, candidate, target).ratio()
            if ratio < RATIO:
                continue
            if best is None or ratio > best[0] + 1e-9:
                best = (ratio, count, target, is_learned)
    if best is None:
        return WakeMatch(False)
    ratio, consumed, target, is_learned = best
    command_tokens = tokens[consumed:]
    while command_tokens and command_tokens[0] in FILLER_WORDS:
        command_tokens.pop(0)
    command = normalise_spoken_command(" ".join(command_tokens).strip())
    return WakeMatch(True, command, ratio, target, is_learned)


def split_wake_word(
    text: str, wake_word: str = "ultron", aliases: str | tuple[str, ...] | list[str] = ""
) -> tuple[bool, str]:
    """Return (wake_word_found, command_text_after_it)."""
    match = wake_match(text, wake_word, aliases)
    return match.found, match.command


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
