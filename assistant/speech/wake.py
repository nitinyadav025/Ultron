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

DEFAULT_ALIASES = ("ultra", "hey ultron")
RATIO = 0.75


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
    return [name for name in dict.fromkeys(cleaned) if name]


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
    targets: list[tuple[str, bool]] = [(name, False) for name in builtin]
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
    return WakeMatch(True, " ".join(tokens[consumed:]).strip(), ratio, target, is_learned)


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
