"""Web tools: URL building, safety, the real-challenge fallback and reply text."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from assistant.brain.tool_replies import open_first_result_reply, web_search_reply
from assistant.tools import web

FIXTURES = Path(__file__).parent / "fixtures"


def test_query_is_cleaned_and_capped() -> None:
    assert web.clean_query("  github\n\x00search  ") == "github search"
    assert len(web.clean_query("x" * 500)) == 200


def test_search_url_is_encoded() -> None:
    url = web.search_url("github cli & tools")
    assert url.startswith("https://duckduckgo.com/?q=")
    assert " " not in url and "&" not in url.split("?q=")[1]


def test_parser_reads_the_first_real_result() -> None:
    page = (FIXTURES / "ddg_results.html").read_text(encoding="utf-8")
    assert web.parse_first_result(page) == "https://github.com/"


def test_parser_returns_nothing_on_the_real_bot_challenge() -> None:
    # The trimmed first part of the page DuckDuckGo really returned on 2026-11-01
    # (HTTP 202, "Unfortunately, bots use DuckDuckGo too."). No personal data.
    page = (FIXTURES / "ddg_challenge.html").read_text(encoding="utf-8")
    assert web.parse_first_result(page) == ""


def test_redirect_is_unwrapped() -> None:
    assert web.unwrap_result_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2F") == "https://github.com/"
    assert web.unwrap_result_url("https://example.org/a") == "https://example.org/a"


def test_non_http_urls_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not launch"))
    assert web.open_url("file:///etc/passwd")["ok"] is False
    assert web.open_url("javascript:alert(1)")["ok"] is False


def test_url_is_opened_with_an_argument_list_never_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProc:
        pid = 1234

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        calls.append((argv, kwargs))
        return FakeProc()

    monkeypatch.setattr(web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(web.subprocess, "Popen", fake_popen)

    result = web.open_url("https://example.org/page")

    assert result["ok"] is True
    argv, kwargs = calls[0]
    assert argv == ["xdg-open", "https://example.org/page"]
    assert isinstance(argv, list)
    assert "shell" not in kwargs or kwargs["shell"] is False


def test_named_browser_receives_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class FakeProc:
        pid = 1

    monkeypatch.setattr(web, "resolve_app_launch_command", lambda name: (["firefox"], "firefox"))
    monkeypatch.setattr(web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(web.subprocess, "Popen", lambda argv, **_k: calls.append(argv) or FakeProc())

    result = web.open_url("https://example.org", browser="firefox")

    assert result["ok"] is True
    assert calls[0] == ["firefox", "https://example.org"]


def test_web_search_builds_the_reply_in_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: {"ok": True, "url": url, "opened_with": "Firefox"})

    result = web.web_search("github", "firefox")

    assert result["ok"] is True
    assert web_search_reply({"ok": True, "raw_summary": result}) == "Searching for github in Firefox."


def test_open_first_result_uses_ducky_without_scraping(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setenv("FIRST_RESULT_METHOD", "ducky")
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})
    monkeypatch.setattr(web, "fetch_first_result", lambda _q: pytest.fail("must not scrape"))

    result = web.open_first_result("github")

    assert opened == [web.DUCKY_URL + "github"]
    assert result["method"] == "ducky"
    assert result["reply"] == "Opening the first result for github."
    assert open_first_result_reply({"ok": True, "raw_summary": result}) == "Opening the first result for github."


def test_google_lucky_and_plain_search_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})

    monkeypatch.setenv("FIRST_RESULT_METHOD", "google")
    google = web.open_first_result("github")
    assert opened[-1] == web.GOOGLE_LUCKY_URL + "github&btnI=1"
    assert google["reply"] == "Opening the first result for github."

    monkeypatch.setenv("FIRST_RESULT_METHOD", "search")
    search = web.open_first_result("github")
    assert opened[-1] == web.search_url("github")
    assert search["reply"] == "Opening the search results for github."


def test_unknown_first_result_method_falls_back_to_ducky(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_RESULT_METHOD", "nonsense")
    url, method = web.first_result_url("github")
    assert method == "ducky" and url.startswith(web.DUCKY_URL)


def test_first_result_reports_a_browser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "open_url", lambda *_a, **_k: {"ok": False, "error": "no browser"})
    result = web.open_first_result("github")
    assert result["ok"] is False
    assert open_first_result_reply({"ok": False, "raw_summary": result}) == web.FIRST_RESULT_FAILED_REPLY


def test_empty_query_opens_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "open_url", lambda *_a, **_k: pytest.fail("must not open"))
    assert web.web_search("   ")["ok"] is False
    assert web.open_first_result("")["ok"] is False
