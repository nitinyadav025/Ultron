"""Web tools: Google default, site/in-tab search, first result, safety and replies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from assistant.brain.context import SESSION
from assistant.brain.tool_replies import open_first_result_reply, web_search_reply
from assistant.tools import web

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_session(monkeypatch: pytest.MonkeyPatch) -> None:
    SESSION.reset()
    monkeypatch.delenv("SEARCH_ENGINE", raising=False)
    monkeypatch.delenv("FIRST_RESULT_METHOD", raising=False)


def test_query_is_cleaned_and_capped() -> None:
    assert web.clean_query("  github\n\x00search  ") == "github search"
    assert len(web.clean_query("x" * 500)) == 200


def test_google_is_the_default_search_engine() -> None:
    url = web.search_url("github cli & tools")
    assert url.startswith("https://www.google.com/search?q=")
    assert " " not in url and "&" not in url.split("?q=")[1]


def test_site_search_urls_are_built_in_code() -> None:
    assert web.site_search_url("youtube", "f1") == "https://www.youtube.com/results?search_query=f1"
    assert web.site_search_url("github", "ultron") == "https://github.com/search?q=ultron"
    assert web.site_search_url("reddit", "python") == "https://www.reddit.com/search/?q=python"
    assert web.site_search_url("wikipedia", "Ultron") == (
        "https://en.wikipedia.org/w/index.php?search=Ultron"
    )
    assert web.site_search_url("not-a-site", "x") == web.search_url("x")


def test_parser_reads_the_first_real_result() -> None:
    page = (FIXTURES / "ddg_results.html").read_text(encoding="utf-8")
    assert web.parse_first_result(page) == "https://github.com/"


def test_parser_returns_nothing_on_the_real_bot_challenge() -> None:
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


def test_web_search_builds_a_natural_reply_in_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: {"ok": True, "url": url, "opened_with": "Firefox"})

    result = web.web_search("github", "firefox")

    assert result["ok"] is True
    assert result["url"] == "https://www.google.com/search?q=github"
    assert web_search_reply({"ok": True, "raw_summary": result}) == (
        "Searching the web for github in Firefox right now."
    )


def test_search_in_youtube_uses_the_site_url(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})

    result = web.web_search("f1", browser="firefox", site="youtube")

    assert result["ok"] is True
    assert opened == ["https://www.youtube.com/results?search_query=f1"]
    assert result["site"] == "youtube"
    assert result["in_current_tab"] is False
    assert web_search_reply({"ok": True, "raw_summary": result}) == (
        "Searching YouTube for f1 right now."
    )


def test_search_uses_the_site_already_on_screen_in_the_current_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        web,
        "_open_in_current_tab",
        lambda url, browser: handled.append((url, browser)) or {"ok": True, "method": "current_tab"},
    )
    SESSION.active_site = "youtube"
    SESSION.last_browser = "firefox"

    result = web.web_search("f1")

    assert handled == [("https://www.youtube.com/results?search_query=f1", "firefox")]
    assert result["in_current_tab"] is True
    assert result["site"] == "youtube"


def test_open_first_result_opens_a_canonical_brand_without_scraping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})
    monkeypatch.setattr(web, "fetch_first_result", lambda _q: pytest.fail("must not scrape a brand"))

    result = web.open_first_result("github")

    assert opened == ["https://github.com"]
    assert result["method"] == "canonical"
    assert result["reply"] == "Opening GitHub for you."


def test_open_first_result_uses_a_clean_fetch_or_google_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web, "open_url", lambda url, browser=None: opened.append(url) or {"ok": True})
    monkeypatch.setattr(web, "fetch_first_result", lambda _q: "https://example.org/python")

    first = web.open_first_result("python tutorial")
    assert opened[-1] == "https://example.org/python"
    assert first["method"] == "first"
    assert first["reply"] == "Opening the first result for python tutorial."

    monkeypatch.setattr(web, "fetch_first_result", lambda _q: "")
    fallback = web.open_first_result("python tutorial")
    assert opened[-1] == "https://www.google.com/search?q=python+tutorial"
    assert fallback["method"] == "search"
    assert fallback["reply"] == web.FIRST_RESULT_FAILED_REPLY


def test_first_result_url_never_uses_btnI() -> None:
    assert web.first_result_url("github") == ("https://github.com", "canonical")
    url, method = web.first_result_url("python tutorial")
    assert method == "search" and url.startswith("https://www.google.com/search?q=")
    assert "btnI" not in url


def test_youtube_first_video_parses_a_video_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web,
        "_fetch_page",
        lambda _url, _timeout=8.0: '{"videoId":"abcDEF12345","title":"F1"}',
    )
    assert web.fetch_youtube_first_video("f1") == "https://www.youtube.com/watch?v=abcDEF12345"


def test_first_result_reports_a_browser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "open_url", lambda *_a, **_k: {"ok": False, "error": "no browser"})
    result = web.open_first_result("github")
    assert result["ok"] is False
    assert open_first_result_reply({"ok": False, "raw_summary": result}) == "I couldn't open the search."


def test_empty_query_opens_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "open_url", lambda *_a, **_k: pytest.fail("must not open"))
    assert web.web_search("   ")["ok"] is False
    assert web.open_first_result("")["ok"] is False
