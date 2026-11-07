"""Reply shaping for the local backend: one short plain sentence, no markdown, no invented details."""

from __future__ import annotations

import re

# Anything that looks like a file size must never be restated from a listing
# ("9 TB" instead of "GB" was the bug this guards against).
_SIZE_PATTERN = re.compile(r"\(?\b\d+(?:\.\d+)?\s*(?:[KMGTPE]i?B|bytes?)\b\)?", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_TRAILING_FILLER = re.compile(
    r"(?i)[,;: ]+(?:and|or|plus|with|of|the|a|an|such as|like|including|containing|which|that)\s*$"
)
_TERMINAL = (".", "!", "?")
_SPECIAL_TOKEN = re.compile(r"<\|[^>\n]*\|>")
_UNTERMINATED_TOKEN = re.compile(r"<\|[A-Za-z0-9_]*")
_LLAMA_TAG = re.compile(r"</?s>")
_STRAY_ANGLE = re.compile(r"(?<!\w)<(?=\s|[A-Za-z])")

# A reply cut off by the token limit often ends on one of these words. The list is the
# one the live logs showed ("...goals on.", "...use for.") plus the usual connectors.
_DANGLING_TAIL = frozenset(
    {
        "on",
        "for",
        "to",
        "with",
        "and",
        "or",
        "the",
        "a",
        "an",
        "in",
        "of",
        "by",
        "at",
        "from",
        "that",
        "as",
        "into",
        "about",
        "but",
    }
)
_CLAUSE_BREAK = re.compile(r"[,;:]\s+")


def _ends_with_terminal(text: str) -> bool:
    return bool(text) and text.rstrip()[-1:] in _TERMINAL


def _finish_sentence(text: str) -> str:
    cleaned = text.rstrip(" ,;:-")
    if not cleaned:
        return ""
    return cleaned if _ends_with_terminal(cleaned) else cleaned + "."


def clean_llm_reply(text: str) -> str:
    """Clean a local model reply and repair a sentence cut off by the token limit.

    A reply that ends with a dangling preposition or conjunction ("...goals on.",
    "...use for.") is trimmed back to the last complete sentence, then the last complete
    clause, and finally to the last non-dangling word. The result always ends with clean
    punctuation, so Piper never speaks a half sentence.
    """
    cleaned = clean_model_text(text)
    if not cleaned:
        return ""
    # NEVER trim a reply that already ends with terminal punctuation: the sentence is
    # complete, even if its last word is "for"/"on"/"with". The old dangling-word rule
    # turned "I see you have a page open for." into "I see you have a page open." and
    # valid prose into fragments. Only unterminated output is repaired.
    if _ends_with_terminal(cleaned):
        return cleaned
    words = cleaned.split()
    if not words:
        return ""
    tail = words[-1].strip(".,;:!?\"'").lower()
    if tail not in _DANGLING_TAIL:
        return _finish_sentence(cleaned)

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    if len(sentences) > 1:
        kept = " ".join(sentences[:-1]).strip()
        if kept and _ends_with_terminal(kept):
            return kept

    breaks = list(_CLAUSE_BREAK.finditer(cleaned))
    if breaks:
        clause = cleaned[: breaks[-1].start()].strip()
        if clause:
            return _finish_sentence(clause)

    trimmed = list(words)
    while trimmed and trimmed[-1].strip(".,;:!?\"'").lower() in _DANGLING_TAIL:
        trimmed.pop()
    return _finish_sentence(" ".join(trimmed))


def _sentence_key(sentence: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()


def clean_model_text(text: str) -> str:
    """Remove stray special-token fragments and collapse an immediately repeated sentence."""
    if not text:
        return ""
    cleaned = _SPECIAL_TOKEN.sub(" ", text)
    cleaned = _UNTERMINATED_TOKEN.sub(" ", cleaned)
    cleaned = _LLAMA_TAG.sub(" ", cleaned)
    cleaned = _STRAY_ANGLE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    kept: list[str] = []
    for sentence in sentences:
        key = _sentence_key(sentence)
        if kept and key and key == _sentence_key(kept[-1]):
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def plain_text(text: str) -> str:
    """Strip markdown, code, paths and size mentions from a model reply."""
    if not text:
        return ""
    cleaned = text
    cleaned = re.sub(r"<untrusted_tool_output[^>]*>.*?</untrusted_tool_output>", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"```.*?```", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"`[^`]*`", " ", cleaned)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*", "", cleaned)
    # Collapse bullet lists into their prose lead-in: drop the list items themselves.
    cleaned = re.sub(r"(?m)^[ \t]*(?:[-*+\u2022]|\d+[.)])[ \t]+.*$", " ", cleaned)
    cleaned = re.sub(r"[*_~#|>]", "", cleaned)
    cleaned = re.sub(r"(?:~|\.{0,2}/)[A-Za-z0-9_\-./]*", " ", cleaned)
    cleaned = _SIZE_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
    return clean_model_text(cleaned)


def short_plain_reply(text: str, max_words: int = 20, max_chars: int = 120) -> str:
    """Reduce a reply to one short, plain, speakable sentence (local backend)."""
    cleaned = plain_text(text)
    if not cleaned:
        return "Done."

    first = _SENTENCE_SPLIT.split(cleaned, maxsplit=1)[0].strip()
    # A bulleted answer usually ends with a colon ("... contains 43 items:"): stop there.
    if ":" in first:
        first = first.split(":", 1)[0].strip()
    first = first.rstrip(" ,;:-")

    words = first.split()
    if len(words) > max_words:
        first = " ".join(words[:max_words])
    if len(first) > max_chars:
        first = first[:max_chars].rsplit(" ", 1)[0]

    first = _TRAILING_FILLER.sub("", first).strip(" ,;:-")
    if not first:
        return "Done."
    if not first.endswith(_TERMINAL):
        first += "."
    return first
