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
SAFE_DEFAULT_ALIASES = ("outron", "ultra", "hey ultron", "here ultron", "hailtone")
# Phonetic variants measured in the user's own wake-train clips (Nov 1, 18:01) plus the
# Nov 2 "I'll turn open VS code" miss. These are ordinary English words, so a match is
# trusted only when the remainder is empty or a known fast-path command (handled by
# ``learned=True`` in WakeMatch).
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
    "i'll turn",
    "ill turn",
    "here drone",
    # Nov 2/3 live mishearings of "Ultron". Ordinary English words, so they are trusted
    # only when the rest is empty or a real fast-path command (learned=True).
    "hey everyone",
    "iron",
    "here drawn",
    "altron",
    "all drawn",
)
DEFAULT_ALIASES = SAFE_DEFAULT_ALIASES + RISKY_ALIASES
RATIO = 0.75
# Whisper inserts small connector words after a misheard wake word:
# "I'll draw an open Firefox." must become "open firefox".
FILLER_WORDS = {"an", "and", "to", "uh", "um", "please", "then", "so"}
# Prefixes wake-train must never save. The Nov 2 file contained "i didn", "i didn t",
# "i ll", "i m", "what do", "what do you" and "hey all"; the first one made the error
# string "I didn't hear anything." match the wake word at 0.86.
BAD_LEARNED_ALIASES = frozenset(
    {
        "i",
        "hey",
        "what",
        "the",
        "and",
        "a",
        "to",
        "you",
        "it",
        "is",
        "so",
        "i didn",
        "i didn t",
        "i ll",
        "i m",
        "what do",
        "what do you",
        "hey all",
        "i don",
        "i don t",
        "i can",
        "i can t",
        "i will",
        "i would",
        "i could",
        "i won",
        "i won t",
        "i was",
        "i am",
        "i have",
        "i had",
    }
)
_CONTRACTION_TAILS = frozenset(
    {"didn", "didn t", "ll", "m", "re", "ve", "s", "t", "don", "don t", "can", "can t", "won", "won t"}
)
_GENERIC_STARTERS = frozenset({"i", "hey", "what", "the", "and", "a", "to", "you", "it", "is", "so"})
# Common app mishearings in the saved clips (B3 wake-train table).
_SPOKEN_COMMAND_FIXES = (
    # Firefox: live Whisper near-misses ("fightform", "5ox", "farfox"…).
    (
        re.compile(
            r"\b(?:fight\s*form|fightform|fire\s*form|fire\s*box|fire\s*fox|"
            r"far\s*fox|farfox|five\s*fox|file\s*box|5ox|5x)\b",
            re.I,
        ),
        "firefox",
    ),
    # Brave/Chrome only when the sentence is clearly an app action, so ordinary words
    # ("wave", "break") are never rewritten in normal dictation.
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:break|babe|wave|rave)\b", re.I),
        r"\1 brave",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:crome|chrom|krome)\b", re.I),
        r"\1 chrome",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:dis\s*cord|disc\s*cord)\b", re.I),
        r"\1 discord",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:her\s*mess|hermez)\b", re.I),
        r"\1 hermes",
    ),
    (
        re.compile(
            r"\b(open|launch|close|switch|focus)\s+"
            r"(?:loot\s*risk|lootris|lu\s*tris|loo\s*tris|loot\s*res)\b",
            re.I,
        ),
        r"\1 lutris",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:calcu\s*later|calculater)\b", re.I),
        r"\1 calculator",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:text\s*editer|text\s*editor)\b", re.I),
        r"\1 text editor",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:term\s*nl|terminel|terminal)\b", re.I),
        r"\1 terminal",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:setting|settings)\b", re.I),
        r"\1 settings",
    ),
    (
        re.compile(r"\b(open|launch|close|switch|focus)\s+(?:file|files)\b", re.I),
        r"\1 files",
    ),
    # Phase 8 screen commands: Indian-English / Whisper near-misses.
    (re.compile(r"\bexpand(?: the)?\s+show\s+more(?:\s+section)?\b", re.I), "click show more"),
    (re.compile(r"\bshow\s+(?:moor|moore)\b", re.I), "show more"),
    (re.compile(r"\bshowmore\b", re.I), "show more"),
    (re.compile(r"\b(?:take\s+)?a?\s*screen\s+shot\s*$", re.I), "take screenshot"),
    (re.compile(r"\bscreen\s+shot\b", re.I), "screenshot"),
    # "Dis cord" and "loot risk" are the measured spoken forms of these app names.
    (re.compile(r"\bdis\s+cord\b", re.I), "discord"),
    (re.compile(r"\b(?:loo\s*tris|lou\s*tris|loot\s*risk|loot\s*res)\b", re.I), "lutris"),
    (re.compile(r"\b(open|launch) (?:5ox|5x|file box|fire box|fire fox)\b", re.I), r"\1 firefox"),
    # "Third YouTube" / "Hurt YouTube" / "Sir YouTube" = "search YouTube".
    (
        re.compile(r"^\s*(?:please\s+)?(?:third|hurt|sir)\s+youtube\s+(.+)$", re.I),
        r"search \1 in youtube",
    ),
    (re.compile(r"\b(?:third|hurt|sir)\s+youtube\b", re.I), "search youtube"),
    # Whisper homophones for "write": "right it", "yes right it", "okay right <topic>".
    # Specific forms run first so "okay right it" is not read as the topic "it".
    (re.compile(r"^\s*(?:okay|ok)[, ]+yes[, ]+right(?:\s+it)?$", re.I), "yes write it"),
    (re.compile(r"^\s*yes[, ]+right(?:\s+it)?$", re.I), "yes write it"),
    (re.compile(r"^\s*(?:okay|ok)[, ]+right(?:\s+it)?$", re.I), "write it"),
    (re.compile(r"^\s*(?:okay|ok)[, ]+right\s+(.+)$", re.I), r"write \1"),
    # "Right hello" in a typing context = "write hello". Ordinary phrases stay unchanged.
    (
        re.compile(r"^\s*right\s+(?!(?:now|away|here|there|back)\b)(.+)$", re.I),
        r"write \1",
    ),
    # A bare "right" after the wake word is the write-it homophone.
    (re.compile(r"^\s*right\s*$", re.I), "write it"),
)


def normalise_spoken_command(text: str) -> str:
    """Repair known Whisper app mishearings and collapse spaces."""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    for pattern, replacement in _SPOKEN_COMMAND_FIXES:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


# Conversational lead-ins that carry no command meaning. They are stripped before the
# fast-path/brain sees the text, so "I want you to open Firefox" becomes "open Firefox".
_CONVERSATIONAL_LEADS = re.compile(
    r"^\s*(?:(?:hey|hi|hello|okay|ok|alright|all\s+right|so)\s*[,:]?\s+)*"
    r"(?:(?:i\s+want\s+you\s+to|i['’]d\s+like\s+you\s+to|i\s+would\s+like\s+you\s+to|"
    r"i\s+need\s+you\s+to|can\s+you(?:\s+please)?|could\s+you(?:\s+please)?|"
    r"would\s+you(?:\s+please)?|will\s+you(?:\s+please)?|go\s+ahead\s+and|"
    r"please|just|now|right\s+now|okay|ok|alright|all\s+right|so)\s+)",
    re.I,
)
_TRAILING_FILLERS = re.compile(
    r"(?:[\s,]+(?:for\s+me|please|right\s+now|now|okay|ok|thanks|thank\s+you))+\s*$",
    re.I,
)


def strip_conversational_prefix(
    text: str,
    wake_word: str = "ultron",
    aliases: str | tuple[str, ...] | list[str] = "",
    learned_aliases: list[str] | tuple[str, ...] = (),
) -> str:
    """Strip wake spellings, polite lead-ins and trailing fillers, recursively.

    "I want you to open Firefox for me." → "open Firefox"
    "Okay, can you please open Hermes?"  → "open Hermes"
    """
    remaining = (text or "").strip()
    for _ in range(8):
        before = remaining
        match = wake_match(remaining, wake_word, aliases, learned_aliases)
        if match.found and match.command and match.command != remaining:
            remaining = match.command.strip()
        remaining = _CONVERSATIONAL_LEADS.sub("", remaining).strip()
        remaining = _TRAILING_FILLERS.sub("", remaining).strip()
        remaining = re.sub(r"\s+", " ", remaining).strip(" ,.;:!?")
        if remaining == before:
            break
    return remaining


# A spoken command that is safe to execute with no wake word in wake_word mode. The
# environment must never answer "Ignored speech (no wake word)" to a clear request like
# "Open Firefox" or "What is on my screen".
_DIRECT_COMMAND_STARTS = (
    "open ",
    "launch ",
    "start ",
    "close ",
    "quit ",
    "switch ",
    "focus ",
    "search ",
    "find ",
    "google ",
    "scroll ",
    "type ",
    "write ",
    "click ",
    "press ",
    "expand ",
    "mute",
    "unmute",
    "set volume",
    "set a timer",
    "set timer",
    "take screenshot",
    "take a screenshot",
    "capture screen",
    "capture the screen",
    "read screen",
    "read the screen",
    "read my screen",
    "what is on my screen",
    "what's on my screen",
    "tell me the date",
    "tell me the time",
    "what time is it",
    "what is the date",
    "stop listening",
    "play ",
    "pause",
    "next song",
    "previous song",
    "list files",
    "show files",
    "new tab",
    "next tab",
    "previous tab",
    "close tab",
    "show more",
    "system status",
)


def is_actionable_command(
    text: str,
    wake_word: str = "ultron",
    aliases: str | tuple[str, ...] | list[str] = "",
    learned_aliases: list[str] | tuple[str, ...] = (),
) -> bool:
    """True when the utterance is a clear command after stripping conversational fluff."""
    cleaned = strip_conversational_prefix(
        normalise_spoken_command(text), wake_word, aliases, learned_aliases
    ).lower()
    if not cleaned:
        return False
    return any(cleaned == prefix.strip() or cleaned.startswith(prefix) for prefix in _DIRECT_COMMAND_STARTS)


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


def is_safe_learned_alias(name: str) -> bool:
    """False for wake-train junk that would wake on ordinary English.

    Rejected: an empty/rejected decode, common words, contraction fragments ("i didn",
    "i ll", "i m"), and anything starting with "what do", "i ll", "i m", "i didn",
    "i don" or "hey all". The measured built-ins ("i'll draw", "i'm drawn") are added
    separately and are not affected by this filter.
    """
    cleaned = " ".join(str(name or "").lower().split())
    if not cleaned or "<rejected" in cleaned or cleaned.startswith("rejected"):
        return False
    if cleaned in BAD_LEARNED_ALIASES:
        return False
    words = cleaned.split()
    if not words:
        return False
    if words[-1] in _CONTRACTION_TAILS:
        return False
    if cleaned.startswith(("what do", "i ll", "i m", "i didn", "i don", "hey all")):
        return False
    if all(word in _GENERIC_STARTERS for word in words):
        return False
    if len(words) == 1 and len(cleaned) < 5 and cleaned not in {"outron", "ultra", "hailtone"}:
        return False
    return True


def load_learned_aliases(path: Path | str | None) -> list[str]:
    """Read workspace/wake_aliases.json (written by `main.py wake-train`).

    Accepts {"aliases": {"ultron": 12, ...}} or {"aliases": ["ultron", ...]}.
    A missing or broken file means "no learned aliases", never an error. The Nov 2 file
    was filtered here too, so an old polluted file cannot bring the error loop back.
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
    return [name for name in dict.fromkeys(cleaned) if is_safe_learned_alias(name)]


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


def _strip_leading_fillers(tokens: list[str]) -> list[str]:
    """Remove repeated fillers and "can you" after the wake prefix."""
    changed = True
    while tokens and changed:
        changed = False
        while tokens and tokens[0] in FILLER_WORDS:
            tokens.pop(0)
            changed = True
        if len(tokens) >= 2 and tokens[0] == "can" and tokens[1] == "you":
            del tokens[:2]
            changed = True
    return tokens


def wake_match(
    text: str,
    wake_word: str = "ultron",
    aliases: str | tuple[str, ...] | list[str] = "",
    learned_aliases: list[str] | tuple[str, ...] = (),
) -> WakeMatch:
    """Return the best wake-word match, including its score and target (for debug output).

    Built-in/configured spellings may be one or two words; learned spellings up to three.
    On a tie the shorter match wins so a real command word is not swallowed. Whenever a
    prefix matches, its words and any leading filler are always removed from ``command``.
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
    command_tokens = _strip_leading_fillers(tokens[consumed:])
    command = normalise_spoken_command(" ".join(command_tokens).strip())
    return WakeMatch(True, command, ratio, target, is_learned)


def wake_match_anywhere(
    text: str,
    wake_word: str = "ultron",
    aliases: str | tuple[str, ...] | list[str] = "",
    learned_aliases: list[str] | tuple[str, ...] = (),
) -> WakeMatch:
    """Find a wake spelling later in the sentence and join both sides with "and".

    The Nov 2 log merged two commands into one 30 s transcript:
    ``"Open Steam, Ultron can you open Discord for me?"``. The prefix matcher cannot see
    the second wake word. This returns ``"open steam and can you open discord for me"`` so
    the fast-path compound splitter can run both actions. The caller accepts it only when
    the remainder is a known fast-path sentence, so ordinary talk about Ultron is ignored.
    """
    tokens = _tokens(text)
    if not tokens:
        return WakeMatch(False)
    builtin = _alias_list(wake_word, aliases)
    learned = [" ".join(str(item).lower().split()) for item in learned_aliases if str(item).strip()]
    restricted = set(RISKY_ALIASES)
    targets: list[tuple[str, bool]] = [(name, name in restricted) for name in builtin]
    targets += [(name, True) for name in learned if name not in builtin]
    best: tuple[float, int, int, str, bool] | None = None
    for index in range(len(tokens)):
        for count in (1, 2, 3):
            if index + count > len(tokens):
                break
            candidate = " ".join(tokens[index : index + count])
            for target, is_learned in targets:
                if len(target.split()) != count:
                    continue
                ratio = difflib.SequenceMatcher(None, candidate, target).ratio()
                if ratio < RATIO:
                    continue
                if best is None or ratio > best[0] + 1e-9:
                    best = (ratio, index, count, target, is_learned)
    if best is None:
        return WakeMatch(False)
    ratio, index, count, target, is_learned = best
    before = tokens[:index]
    after = tokens[index + count :]
    joined = before + (["and"] if before and after else []) + after
    command = normalise_spoken_command(" ".join(joined).strip())
    return WakeMatch(True, command, ratio, target, is_learned)


def split_wake_word(
    text: str, wake_word: str = "ultron", aliases: str | tuple[str, ...] | list[str] = ""
) -> tuple[bool, str]:
    """Return (wake_word_found, command_text_after_it)."""
    match = wake_match(text, wake_word, aliases)
    return match.found, match.command


CONTINUATION_WORDS = {
    "and",
    "then",
    "the",
    "a",
    "an",
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
    "so",
    "that",
    "because",
    "like",
    "search",
    "open",
    "close",
    "type",
    "write",
    "can",
    "you",
    "please",
    "about",
    "from",
    "into",
    "at",
}
DANGLING_WORDS = CONTINUATION_WORDS


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
