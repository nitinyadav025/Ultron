"""Web search helpers: open a search page, a site search, or the first real result.

Actions:

* ``web_search(query, browser=None, site=None, in_current_tab=False)`` builds the URL
  in code and opens it. Known sites use their own search URL; a site already on screen is
  searched in the active tab instead of spawning a duplicate tab.
* ``open_first_result(query, ...)`` never uses Google's ``btnI=1`` (it showed a blank
  redirect page). Known brands open their canonical site, YouTube can open its first
  parsed video, and other queries use a clean first-result fetch or the Google results.

Safety: subprocess argument lists only (never ``shell=True``), only http/https URLs,
queries capped at 200 characters, and nothing is typed outside the in-tab helper.
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

from assistant.brain.context import (
    SESSION,
    active_search_site,
    get_screen_context,
)
from assistant.tools.apps import resolve_app_launch_command, resolve_known_app
from assistant.tools.registry import tool

logger = logging.getLogger("ultron.web")

SEARCH_ENGINES = {
    "duckduckgo": "https://duckduckgo.com/?q=",
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
}
DEFAULT_ENGINE = "google"

SITE_SEARCH_URLS: dict[str, str] = {
    "youtube": "https://www.youtube.com/results?search_query=",
    "github": "https://github.com/search?q=",
    "reddit": "https://www.reddit.com/search/?q=",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
    "google": "https://www.google.com/search?q=",
    "amazon": "https://www.amazon.com/s?k=",
    "spotify": "https://open.spotify.com/search/",
    "gmail": "https://mail.google.com/mail/u/0/#search/",
}

CANONICAL_SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "instagram": "https://www.instagram.com",
    "discord": "https://discord.com/app",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://en.wikipedia.org",
    "chatgpt": "https://chatgpt.com",
    "gmail": "https://mail.google.com",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "whatsapp": "https://web.whatsapp.com",
    "amazon": "https://www.amazon.com",
    "linkedin": "https://www.linkedin.com",
    "twitter": "https://x.com",
    "google": "https://www.google.com",
    "claude": "https://claude.ai",
    # Expanded canonical brands: opening these directly avoids a search redirect.
    "lutris": "https://lutris.net",
    "f1": "https://www.formula1.com",
    "formula 1": "https://www.formula1.com",
    "formula one": "https://www.formula1.com",
    "steam": "https://store.steampowered.com",
    "gmail mail": "https://mail.google.com",
}

DISPLAY_NAMES: dict[str, str] = {
    "youtube": "YouTube",
    "github": "GitHub",
    "instagram": "Instagram",
    "discord": "Discord",
    "reddit": "Reddit",
    "wikipedia": "Wikipedia",
    "chatgpt": "ChatGPT",
    "gmail": "Gmail",
    "netflix": "Netflix",
    "spotify": "Spotify",
    "whatsapp": "WhatsApp",
    "amazon": "Amazon",
    "linkedin": "LinkedIn",
    "twitter": "X",
    "google": "Google",
    "claude": "Claude",
    "lutris": "Lutris",
    "f1": "F1",
    "formula 1": "Formula 1",
    "steam": "Steam",
}

SITE_ALIASES: dict[str, str] = {
    "you tube": "youtube",
    "youtube.com": "youtube",
    "yt": "youtube",
    "git hub": "github",
    "github.com": "github",
    "insta": "instagram",
    "ig": "instagram",
    "reddit.com": "reddit",
    "wiki": "wikipedia",
    "chat gpt": "chatgpt",
    "google mail": "gmail",
    "gmail.com": "gmail",
    "x twitter": "twitter",
    "x.com": "twitter",
}

GOOGLE_RESULTS_URL = "https://www.google.com/search?q="
# Sites that a spoken "search <site>" is allowed to target. A generic word such as
# "instagram" or "google" is still a normal web query unless the user says "in/on".
REQUEST_SEARCH_SITES = frozenset(
    {"youtube", "github", "reddit", "wikipedia", "discord", "spotify"}
)
DUCKY_URL = "https://duckduckgo.com/?q=!ducky+"
FIRST_RESULT_URL = "https://html.duckduckgo.com/html/?q="
YOUTUBE_SEARCH_URL = SITE_SEARCH_URLS["youtube"]
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
MAX_QUERY = 200
TIMEOUT_S = 8.0
FIRST_RESULT_TIMEOUT_S = 2.5
FIRST_RESULT_FAILED_REPLY = (
    "I couldn't pick the first result, so I opened the Google results."
)
# Both of these render a blank white redirect screen in Firefox/Brave. Never open them.
_BLANK_REDIRECT_MARKERS = ("btni", "!ducky", "ducky+")


def clean_query(query: str) -> str:
    """Replace control characters with spaces and cap the query at 200 characters."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in (query or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MAX_QUERY]


def normalise_site(site: str | None) -> str:
    """Resolve a spoken site name to a key we know how to search, or ''."""
    cleaned = re.sub(r"[^a-z0-9. ]+", " ", (site or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^(?:the|on|in) ", "", cleaned)
    cleaned = SITE_ALIASES.get(cleaned, cleaned)
    if cleaned in SITE_SEARCH_URLS or cleaned in CANONICAL_SITES:
        return cleaned
    if cleaned.endswith(".com") and cleaned[:-4] in SITE_SEARCH_URLS:
        return cleaned[:-4]
    return ""


def search_url(query: str, engine: str | None = None) -> str:
    """Build the normal web-search URL (Google by default)."""
    chosen = (engine or os.environ.get("SEARCH_ENGINE") or DEFAULT_ENGINE).lower()
    base = SEARCH_ENGINES.get(chosen, SEARCH_ENGINES[DEFAULT_ENGINE])
    return base + urllib.parse.quote_plus(clean_query(query))


def site_search_url(site: str, query: str) -> str:
    """Build a search URL inside a known site (YouTube, GitHub, ...).

    An empty query opens the site itself (the task's ``search YouTube`` case). A site
    with no public search URL (Discord, Instagram, ...) searches Google with a
    ``site:`` filter instead of opening a blank search page.
    """
    key = normalise_site(site)
    cleaned = clean_query(query)
    if not key:
        return search_url(cleaned)
    if not cleaned:
        canonical = CANONICAL_SITES.get(key)
        if canonical:
            return canonical
        base = SITE_SEARCH_URLS.get(key, "")
        return base.split("?")[0] if base else search_url(cleaned)
    if key in SITE_SEARCH_URLS:
        return SITE_SEARCH_URLS[key] + urllib.parse.quote_plus(cleaned)
    canonical = CANONICAL_SITES.get(key)
    if canonical:
        domain = urllib.parse.urlparse(canonical).netloc
        return search_url(f"site:{domain} {cleaned}")
    return search_url(cleaned)


def canonical_site_for(query: str) -> str:
    """Return the site URL when the whole query is a known brand, else ''."""
    key = normalise_site(query)
    return CANONICAL_SITES.get(key, "")


def parse_search_request(text: str) -> tuple[str, str] | None:
    """Parse a natural site search into ``(query, site)``.

    Handles the live phrasings ``"search f1 in youtube"``,
    ``"search on youtube for f1"``, ``"search what is server in github"``,
    ``"search youtube"`` (empty query opens the site) and the same forms for Reddit,
    Wikipedia, Discord and Spotify. A browser name (``firefox``) is not a site, so the
    caller's normal browser-search path still applies.
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip()).rstrip(" .!?")
    if not cleaned:
        return None

    on_first = re.match(
        r"(?i)^(?:please\s+)?search\s+(?:on|in)\s+(?P<site>[a-z0-9 .]+?)"
        r"(?:\s+for\s+(?P<query>.+))?$",
        cleaned,
    )
    if on_first:
        site = normalise_site(on_first.group("site"))
        if site in REQUEST_SEARCH_SITES:
            return (on_first.group("query") or "").strip(), site

    in_last = re.match(
        r"(?i)^(?:please\s+)?search\s+(?:for\s+)?(?P<query>.+?)\s+"
        r"(?:on|in)\s+(?P<site>[a-z0-9 .]+)$",
        cleaned,
    )
    if in_last:
        site = normalise_site(in_last.group("site"))
        if site in REQUEST_SEARCH_SITES:
            return in_last.group("query").strip(), site

    bare = re.match(r"(?i)^(?:please\s+)?search\s+(?P<site>[a-z0-9 .]+)$", cleaned)
    if bare:
        site = normalise_site(bare.group("site"))
        if site in REQUEST_SEARCH_SITES:
            return "", site
    return None


def _safe_http_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_blank_redirect(url: str) -> bool:
    """True for the two redirect URLs that render a blank white page.

    Google ``btnI=1`` shows "if you are not redirected..." and DuckDuckGo ``!ducky``
    shows an empty page before its redirect. Neither is ever opened.
    """
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _BLANK_REDIRECT_MARKERS)


def choose_browser(browser: str | None = None) -> str:
    """Explicit browser, else the session's last browser, else Firefox if installed."""
    explicit = (browser or "").strip().lower()
    if explicit:
        return explicit
    if SESSION.last_browser:
        return SESSION.last_browser
    if resolve_known_app("firefox") is not None:
        return "firefox"
    return ""


def open_url(url: str, browser: str | None = None) -> dict[str, Any]:
    """Open a URL with the named browser, else with the system opener (argv lists only)."""
    if not _safe_http_url(url):
        return {"ok": False, "error": f"Refused a non-http(s) URL: {url!r}"}
    if is_blank_redirect(url):
        return {"ok": False, "error": "Refused a blank redirect URL."}
    if browser:
        # resolve_app_launch_command returns an executable that accepts a URL (never
        # `gio launch`, which takes only a .desktop file). Chromium browsers include
        # --force-renderer-accessibility so page buttons/links reach AT-SPI.
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
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {"ok": False, "error": f"Could not open the browser: {exc}"}
    # Brave's WebRTC AGC can boost the mic as soon as the browser starts. Re-apply the
    # safe source level once the browser has settled.
    from assistant.speech.mic_input import schedule_mic_volume_restore

    schedule_mic_volume_restore()
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
        href = (values.get("href") or "").strip()
        if not href:
            return
        # DuckDuckGo result links, Google /url?q= links, and direct http(s) results.
        if (
            "result__a" in classes
            or href.startswith("/url?q=")
            or href.startswith("/url?")
            or "google.com/url?q=" in href
        ):
            self.hrefs.append(href)


def unwrap_result_url(href: str) -> str:
    """Turn a search-engine redirect into the real URL.

    Handles DuckDuckGo ``//duckduckgo.com/l/?uddg=...`` and Google ``/url?q=...`` (both
    keep the target in a query parameter). Direct URLs pass through unchanged.
    """
    url = href.strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        encoded = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        return urllib.parse.unquote(encoded)
    if parsed.path in ("/url", "/url/"):
        encoded = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
        if encoded:
            return urllib.parse.unquote(encoded)
    return url


def parse_first_result(page: str) -> str:
    """Return the first real http(s) result URL in a search HTML page, or ""."""
    parser = _FirstResultParser()
    parser.feed(page)
    for href in parser.hrefs:
        url = unwrap_result_url(href)
        if _safe_http_url(url) and not is_blank_redirect(url):
            return url
    return ""


def _fetch_page(url: str, timeout: float = TIMEOUT_S) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https constant
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, "replace")
    except Exception as exc:  # network, HTTP, timeout, decoding
        logger.warning("Page fetch failed: %s", type(exc).__name__)
        return ""


def fetch_first_result(query: str, timeout: float = FIRST_RESULT_TIMEOUT_S) -> str:
    """Read the results HTML and return the first real URL, or "".

    DuckDuckGo's HTML endpoint is tried first (its markup is stable), then Google. Both
    receive a real browser User-Agent and are capped at 2.5 s so a blocked network can
    never stall a spoken command.
    """
    cleaned = clean_query(query)
    if not cleaned:
        return ""
    encoded = urllib.parse.quote_plus(cleaned)
    for url in (FIRST_RESULT_URL + encoded, GOOGLE_RESULTS_URL + encoded):
        page = _fetch_page(url, timeout)
        if not page:
            continue
        found = parse_first_result(page)
        if found and not is_blank_redirect(found):
            return found
    return ""


def fetch_youtube_first_video(query: str, timeout: float = TIMEOUT_S) -> str:
    """Return the first ordinary YouTube video URL parsed from a search page, or ''."""
    page = _fetch_page(YOUTUBE_SEARCH_URL + urllib.parse.quote_plus(clean_query(query)), timeout)
    found = re.search(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', page or "")
    if not found:
        return ""
    url = f"https://www.youtube.com/watch?v={found.group(1)}"
    return url if _safe_http_url(url) else ""


def domain_of(url: str) -> str:
    netloc = urllib.parse.urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _open_in_current_tab(url: str, browser: str) -> dict[str, Any]:
    from assistant.tools.desktop_control import navigate_current_tab

    navigated = navigate_current_tab(url, browser)
    if navigated.get("ok"):
        return {**navigated, "opened": False}
    return open_url(url, browser or None)


@tool(risk_level="safe", read_only=False)
def web_search(
    query: str,
    browser: str | None = None,
    site: str | None = None,
    in_current_tab: bool = False,
) -> dict[str, Any]:
    """Search the web or a site in a browser."""
    cleaned = clean_query(query)
    # Refresh the live screen once: if the site is already open, search inside it.
    get_screen_context()
    requested_site = normalise_site(site)
    resolved_site = requested_site or active_search_site()
    if not cleaned and not resolved_site:
        return {"ok": False, "error": "The search text was empty."}
    chosen = choose_browser(browser)
    url = site_search_url(resolved_site, cleaned) if resolved_site else search_url(cleaned)

    site_open = bool(resolved_site) and resolved_site in (
        SESSION.active_site,
        SESSION.last_site,
    )
    same_browser = not chosen or not SESSION.last_browser or chosen == SESSION.last_browser
    # "Already in a browser session" includes the browser we opened earlier even if its
    # current title does not name the site (for example a fresh blank tab).
    browser_session = bool(
        chosen and (SESSION.last_browser or SESSION.active_site or SESSION.last_site)
    )
    # In-tab navigation is for a SITE search. A plain Google query still opens the
    # browser normally, so the first search after a launch cannot hijack the old tab.
    use_current_tab = bool(
        chosen
        and resolved_site
        and (in_current_tab or site_open or (browser_session and same_browser))
    )

    if use_current_tab and chosen:
        opened = _open_in_current_tab(url, chosen)
    else:
        opened = open_url(url, chosen or None)
    if not opened.get("ok"):
        return {
            "ok": False,
            "query": cleaned,
            "browser": chosen,
            "site": resolved_site,
            "error": opened.get("error"),
        }
    if chosen:
        SESSION.note_app(chosen, browser=True)
    SESSION.note_search(cleaned, chosen or None)
    if resolved_site:
        SESSION.active_site = resolved_site
        SESSION.last_site = resolved_site
        SESSION.note_action(f"searched {resolved_site} for {cleaned}".strip())
    return {
        "ok": True,
        "query": cleaned,
        "browser": chosen,
        "site": resolved_site,
        "url": url,
        "in_current_tab": bool(opened.get("method") == "current_tab"),
        "opened_with": opened.get("opened_with"),
        "reply": (
            f"Opening {DISPLAY_NAMES.get(resolved_site, resolved_site.title())}."
            if resolved_site and not cleaned
            else None
        ),
    }


def first_result_url(query: str, method: str | None = None) -> tuple[str, str]:
    """Return (url, method) without any redirect trick: canonical site or search page."""
    canonical = canonical_site_for(query)
    if canonical:
        return canonical, "canonical"
    return search_url(query), "search"


@tool(risk_level="safe", read_only=False)
def open_first_result(
    query: str,
    browser: str | None = None,
    site: str | None = None,
    first_video: bool = False,
) -> dict[str, Any]:
    """Search the web and open the first result, or a known site directly."""
    cleaned = clean_query(query)
    if not cleaned:
        return {"ok": False, "error": "The search text was empty."}
    chosen = choose_browser(browser)
    requested_site = normalise_site(site)

    url = ""
    method = ""
    reply = ""
    if first_video or (requested_site == "youtube" and first_video):
        url = fetch_youtube_first_video(cleaned)
        if url:
            method = "youtube_video"
            reply = f"Playing the first YouTube video for {cleaned}."
        else:
            url = site_search_url("youtube", cleaned)
            method = "site_search"
            reply = f"I opened the YouTube results for {cleaned}."
    elif requested_site:
        url = site_search_url(requested_site, cleaned)
        method = "site_search"
        reply = f"Opening the {DISPLAY_NAMES.get(requested_site, requested_site.title())} results for {cleaned}."
    else:
        canonical = canonical_site_for(cleaned)
        if canonical:
            url = canonical
            method = "canonical"
            reply = f"Opening {DISPLAY_NAMES.get(normalise_site(cleaned), cleaned.title())} for you."
        else:
            url = fetch_first_result(cleaned)
            if url:
                method = "first"
                reply = f"Opening the first result for {cleaned}."
            else:
                url = search_url(cleaned)
                method = "search"
                reply = FIRST_RESULT_FAILED_REPLY

    if is_blank_redirect(url):  # defence in depth: never show a white redirect page
        url = search_url(cleaned)
        method = "search"
        reply = FIRST_RESULT_FAILED_REPLY

    site_open = bool(requested_site) and requested_site in (
        SESSION.active_site,
        SESSION.last_site,
    )
    if site_open and chosen and method == "site_search":
        opened = _open_in_current_tab(url, chosen)
    else:
        opened = open_url(url, chosen or None)
    if not opened.get("ok"):
        return {"ok": False, "query": cleaned, "browser": chosen, "error": opened.get("error")}
    if chosen:
        SESSION.note_app(chosen, browser=True)
    SESSION.note_search(cleaned, chosen or None)
    if requested_site:
        SESSION.active_site = requested_site
        SESSION.last_site = requested_site
    return {
        "ok": True,
        "query": cleaned,
        "browser": chosen,
        "site": requested_site,
        "url": url,
        "method": method,
        "reply": reply,
        "in_current_tab": bool(opened.get("method") == "current_tab"),
    }
