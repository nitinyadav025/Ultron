"""Web search helpers: open a search page, or open the first real result.

Only two actions exist:

* ``web_search(query, browser=None)`` builds the URL in code and opens it;
* ``open_first_result(query)`` does ONE read-only HTTPS GET to DuckDuckGo's HTML page,
  picks the first real result, unwraps DuckDuckGo's redirect and opens it.

Safety: subprocess argument lists only (never ``shell=True``), only http/https URLs,
queries capped at 200 characters with control characters removed, and nothing is typed
or pressed.
"""

from __future__ import annotations

import html.parser
import logging
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from typing import Any

from assistant.tools.apps import resolve_app_launch_command
from assistant.tools.registry import tool

logger = logging.getLogger("ultron.web")

SEARCH_ENGINES = {
    "duckduckgo": "https://duckduckgo.com/?q=",
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
}
DEFAULT_ENGINE = "duckduckgo"
FIRST_RESULT_METHODS = ("ducky", "google", "search")
# The user confirmed the Google "I'm feeling lucky" redirect works on this machine.
DEFAULT_FIRST_RESULT_METHOD = "google"
DUCKY_URL = "https://duckduckgo.com/?q=!ducky+"
GOOGLE_LUCKY_URL = "https://www.google.com/search?q="
FIRST_RESULT_URL = "https://html.duckduckgo.com/html/?q="
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Ultron/1.0"
MAX_QUERY = 200
TIMEOUT_S = 8.0
FIRST_RESULT_FAILED_REPLY = (
    "I couldn't pick the first result, so I opened the search results."
)


def clean_query(query: str) -> str:
    """Replace control characters with spaces and cap the query at 200 characters."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in (query or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MAX_QUERY]


def search_url(query: str, engine: str | None = None) -> str:
    """Build the search URL for SEARCH_ENGINE (default duckduckgo)."""
    chosen = (engine or os.environ.get("SEARCH_ENGINE") or DEFAULT_ENGINE).lower()
    base = SEARCH_ENGINES.get(chosen, SEARCH_ENGINES[DEFAULT_ENGINE])
    return base + urllib.parse.quote_plus(clean_query(query))


def _safe_http_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def open_url(url: str, browser: str | None = None) -> dict[str, Any]:
    """Open a URL with the named browser, else with the system opener (argv lists only)."""
    if not _safe_http_url(url):
        return {"ok": False, "error": f"Refused a non-http(s) URL: {url!r}"}
    if browser:
        cmd, resolved = resolve_app_launch_command(browser)
        if not shutil.which(cmd[0]):
            return {"ok": False, "error": f"Browser '{browser}' was not found."}
        argv = [*cmd, url]
        name = resolved or browser
    elif shutil.which("xdg-open"):
        argv, name = ["xdg-open", url], "the default browser"
    elif shutil.which("gio"):
        argv, name = ["gio", "open", url], "the default browser"
    else:
        return {"ok": False, "error": "No URL opener found (xdg-open or gio is missing)."}
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "error": f"Could not open the browser: {exc}"}
    logger.info("Opened %s with %s (pid %s)", url, name, proc.pid)
    return {"ok": True, "url": url, "opened_with": name, "pid": proc.pid}


class _FirstResultParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        href = values.get("href") or ""
        if href and "result__a" in classes:
            self.hrefs.append(href)


def unwrap_result_url(href: str) -> str:
    """Turn DuckDuckGo's ``//duckduckgo.com/l/?uddg=...`` redirect into the real URL."""
    url = href.strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        encoded = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        return urllib.parse.unquote(encoded)
    return url


def parse_first_result(page: str) -> str:
    """Return the first real http(s) result URL in a DuckDuckGo HTML page, or ""."""
    parser = _FirstResultParser()
    parser.feed(page)
    for href in parser.hrefs:
        url = unwrap_result_url(href)
        if _safe_http_url(url):
            return url
    return ""


def fetch_first_result(query: str, timeout: float = TIMEOUT_S) -> str:
    """One read-only HTTPS GET; return the first result URL or ""."""
    url = FIRST_RESULT_URL + urllib.parse.quote_plus(clean_query(query))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https constant
            charset = response.headers.get_content_charset() or "utf-8"
            page = response.read().decode(charset, "replace")
    except Exception as exc:  # network, HTTP, timeout, decoding
        logger.warning("First-result fetch failed: %s", type(exc).__name__)
        return ""
    return parse_first_result(page)


def domain_of(url: str) -> str:
    netloc = urllib.parse.urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


@tool(risk_level="safe", read_only=False)
def web_search(query: str, browser: str | None = None) -> dict[str, Any]:
    """Search the web in a browser."""
    cleaned = clean_query(query)
    if not cleaned:
        return {"ok": False, "error": "The search text was empty."}
    url = search_url(cleaned)
    opened = open_url(url, browser)
    if not opened.get("ok"):
        return {"ok": False, "query": cleaned, "browser": browser, "error": opened.get("error")}
    return {
        "ok": True,
        "query": cleaned,
        "browser": browser or "",
        "url": url,
        "opened_with": opened.get("opened_with"),
    }


def first_result_url(query: str, method: str | None = None) -> tuple[str, str]:
    """Return (url, method). No scraping: the browser follows the redirect."""
    chosen = (method or os.environ.get("FIRST_RESULT_METHOD") or DEFAULT_FIRST_RESULT_METHOD).lower()
    if chosen not in FIRST_RESULT_METHODS:
        chosen = DEFAULT_FIRST_RESULT_METHOD
    encoded = urllib.parse.quote_plus(clean_query(query))
    if chosen == "ducky":
        return DUCKY_URL + encoded, chosen
    if chosen == "google":
        return GOOGLE_LUCKY_URL + encoded + "&btnI=1", chosen
    return search_url(query), chosen


@tool(risk_level="safe", read_only=False)
def open_first_result(query: str) -> dict[str, Any]:
    """Search the web and open the first result."""
    cleaned = clean_query(query)
    if not cleaned:
        return {"ok": False, "error": "The search text was empty."}
    url, method = first_result_url(cleaned)
    opened = open_url(url)
    if not opened.get("ok"):
        return {"ok": False, "query": cleaned, "error": opened.get("error")}
    reply = (
        f"Opening the search results for {cleaned}."
        if method == "search"
        else f"Opening the first result for {cleaned}."
    )
    return {"ok": True, "query": cleaned, "url": url, "method": method, "reply": reply}
