"""Wake-word matching and the dangling-word rule (offline, Piper evidence)."""

from __future__ import annotations

import pytest

from assistant.speech.wake import first_word, sentence_is_complete, split_wake_word


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("Ultron, open firefox", "open firefox"),
        ("ultron mute", "mute"),
        ("Ultra-open Firefox", "open firefox"),  # what Piper+Whisper really wrote
        ("Ultra, open Firefox.", "open firefox"),
        ("Hey Ultron, open firefox.", "open firefox"),
    ],
)
def test_wake_word_and_command_are_split(text: str, command: str) -> None:
    found, rest = split_wake_word(text)
    assert found is True
    assert rest == command


def test_wake_word_alone_has_an_empty_command() -> None:
    assert split_wake_word("Ultron") == (True, "")


@pytest.mark.parametrize("text", ["open firefox", "hey google", "mute", ""])
def test_sentences_without_the_wake_word_do_not_match(text: str) -> None:
    assert split_wake_word(text)[0] is False


def test_custom_aliases_come_from_config() -> None:
    assert split_wake_word("computer open firefox", aliases="computer") == (True, "open firefox")


@pytest.mark.parametrize("text", ["open firefox and", "open firefox,", "list files in the", "then"])
def test_dangling_sentences_are_not_complete(text: str) -> None:
    assert sentence_is_complete(text) is False


@pytest.mark.parametrize("text", ["open firefox", "mute", "list my downloads"])
def test_finished_sentences_are_complete(text: str) -> None:
    assert sentence_is_complete(text) is True


def test_first_word_for_tuning_logs() -> None:
    assert first_word("Open Firefox now") == "open"
    assert first_word("") == ""
