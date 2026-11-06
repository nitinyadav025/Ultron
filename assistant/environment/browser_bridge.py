"""Local Brave/Firefox bridge shared by the HTTP server and the LLM tools.

The extension in ``extensions/ultron-browser-bridge/`` pushes tab/DOM snapshots to
``/api/browser/sync`` every 800 ms and pulls queued DOM actions from ``/api/browser/poll``.
Everything stays on localhost; no page data is sent anywhere else.

The hub also falls back to the CDP endpoint (if the user started Chrome/Brave with
``--remote-debugging-port=9222``) and to the window-title sensor when the extension is not
loaded, so ``list_browser_tabs`` is never empty on a normal desktop.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Iterable

from assistant.environment.state import get_state

logger = logging.getLogger("ultron.environment.browser")

CDP_URL = "http://127.0.0.1:9222/json"
ACTION_TTL_S = 30.0
MAX_ACTIONS = 100


def _clean_tab(browser: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "browser": str(browser or raw.get("browser") or "unknown"),
        "tabId": int(raw.get("tabId") or raw.get("id") or 0),
        "windowId": int(raw.get("windowId") or 0),
        "title": str(raw.get("title") or ""),
        "url": str(raw.get("url") or ""),
        "active": bool(raw.get("active", False)),
        "audible": bool(raw.get("audible", False)),
        "pinned": bool(raw.get("pinned", False)),
    }


class BrowserBridgeHub:
    """Thread-safe tab registry, DOM cache and command queue."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tabs: dict[str, dict[str, dict[str, Any]]] = {}
        self._dom: dict[str, dict[str, Any]] = {}
        self._actions: list[dict[str, Any]] = []
        self._results: dict[str, dict[str, Any]] = {}
        self._last_sync = 0.0
        self._last_browser = ""

    # -- extension sync ----------------------------------------------------------

    def sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        browser = str(payload.get("browser") or "unknown").lower()
        tabs = payload.get("tabs") if isinstance(payload.get("tabs"), list) else []
        with self._lock:
            table: dict[str, dict[str, Any]] = {}
            for raw in tabs:
                if not isinstance(raw, dict):
                    continue
                tab = _clean_tab(browser, raw)
                if not tab["tabId"]:
                    continue
                table[f"{browser}:{tab['tabId']}"] = tab
            if table:
                self._tabs[browser] = table
            dom = payload.get("dom")
            if isinstance(dom, dict) and dom:
                key = self._dom_key(browser, dom)
                self._dom[key] = {
                    **dom,
                    "browser": browser,
                    "ts": time.time(),
                }
            results = payload.get("results")
            if isinstance(results, list):
                for item in results:
                    if isinstance(item, dict) and item.get("id"):
                        self._results[str(item["id"])] = item
                        self._results = dict(list(self._results.items())[-MAX_ACTIONS:])
            self._last_sync = time.time()
            self._last_browser = browser
            tabs_snapshot = self._tabs_snapshot_locked()
        self._publish(tabs_snapshot)
        return {"ok": True, "actions": self.poll(browser), "tab_count": len(tabs_snapshot)}

    def _dom_key(self, browser: str, dom: dict[str, Any]) -> str:
        tab_id = dom.get("tabId") or dom.get("id") or 0
        if tab_id:
            return f"{browser}:{int(tab_id)}"
        # No tab id from a content script: attach to the active tab when we know it.
        for key, tab in self._tabs.get(browser, {}).items():
            if tab.get("active"):
                return key
        return f"{browser}:unknown"

    def poll(self, browser: str = "") -> list[dict[str, Any]]:
        now = time.time()
        wanted = str(browser or "").lower()
        with self._lock:
            out: list[dict[str, Any]] = []
            for action in self._actions:
                if action.get("delivered"):
                    continue
                if now - float(action.get("created") or 0) > ACTION_TTL_S:
                    action["delivered"] = True
                    continue
                if wanted and action.get("browser") and action["browser"] != wanted:
                    continue
                action["delivered"] = True
                out.append(action)
            return out

    # -- tab discovery -----------------------------------------------------------

    def list_tabs(self) -> list[dict[str, Any]]:
        with self._lock:
            tabs = self._tabs_snapshot_locked()
        known = {(tab["browser"], tab["tabId"], tab["title"], tab["url"]) for tab in tabs}
        for cdp in self._cdp_tabs():
            signature = (cdp["browser"], cdp["tabId"], cdp["title"], cdp["url"])
            if signature not in known:
                tabs.append(cdp)
                known.add(signature)
        if not tabs:
            for title in self._wmctrl_titles():
                tabs.append(
                    {
                        "browser": "window",
                        "tabId": 0,
                        "windowId": 0,
                        "title": title,
                        "url": "",
                        "active": True,
                        "audible": False,
                        "pinned": False,
                        "source": "wmctrl",
                    }
                )
        return tabs

    def _tabs_snapshot_locked(self) -> list[dict[str, Any]]:
        tabs: list[dict[str, Any]] = []
        for browser in sorted(self._tabs):
            tabs.extend(sorted(self._tabs[browser].values(), key=lambda tab: tab["tabId"]))
        return [dict(tab) for tab in tabs]

    def _cdp_tabs(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except Exception:  # pragma: no cover - httpx is a hard dependency
            return []
        try:
            response = httpx.get(CDP_URL, timeout=0.4)
            data = response.json()
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        tabs: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            tabs.append(
                {
                    "browser": "chrome-cdp",
                    "tabId": int(item.get("id") or 0) if str(item.get("id") or "").isdigit() else 0,
                    "windowId": 0,
                    "title": str(item.get("title") or url),
                    "url": url,
                    "active": False,
                    "audible": False,
                    "pinned": False,
                    "source": "cdp",
                }
            )
        return tabs

    @staticmethod
    def _wmctrl_titles() -> list[str]:
        if not shutil.which("wmctrl"):
            return []
        try:
            done = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True,
                text=True,
                timeout=0.8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        titles: list[str] = []
        for line in done.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[3].strip():
                titles.append(parts[3].strip())
        return titles

    # -- matching / reading ------------------------------------------------------

    def find_tab(self, query: str) -> dict[str, Any] | None:
        needle = re.sub(r"\s+", " ", (query or "").strip()).lower()
        if not needle:
            return None
        tabs = self.list_tabs()
        best: tuple[int, dict[str, Any]] | None = None
        for tab in tabs:
            title = str(tab.get("title") or "").lower()
            url = str(tab.get("url") or "").lower()
            score = 0
            if needle == title:
                score = 100
            elif needle in title:
                score = 80
            elif needle in url:
                score = 60
            elif all(word in f"{title} {url}" for word in needle.split()):
                score = 40
            if score and (best is None or score > best[0]):
                best = (score, tab)
        return dict(best[1]) if best else None

    def read_tab(self, query: str) -> dict[str, Any]:
        tab = self.find_tab(query)
        if tab is None:
            return {"ok": False, "error": f"I couldn't find an open tab matching {query!r}."}
        key = f"{tab['browser']}:{tab['tabId']}"
        with self._lock:
            dom = dict(self._dom.get(key) or {})
        if not dom:
            return {
                "ok": False,
                "tab": tab,
                "error": "No DOM snapshot for that tab yet; load the Ultron browser bridge extension.",
            }
        special = dom.get("special") if isinstance(dom.get("special"), dict) else {}
        text = str(special.get("text") or dom.get("text") or "").strip()
        return {
            "ok": True,
            "tab": tab,
            "text": text[:8000],
            "special": special,
            "elements": list(dom.get("elements") or [])[:80],
        }

    # -- actions -----------------------------------------------------------------

    def queue_action(
        self,
        action: str,
        *,
        tab: dict[str, Any] | None = None,
        target: str = "",
        text: str = "",
        submit: bool = True,
    ) -> dict[str, Any]:
        action_id = uuid.uuid4().hex[:12]
        payload = {
            "id": action_id,
            "type": action,
            "browser": str((tab or {}).get("browser") or ""),
            "tabId": int((tab or {}).get("tabId") or 0),
            "windowId": int((tab or {}).get("windowId") or 0),
            "target": target,
            "text": text,
            "submit": bool(submit),
            "created": time.time(),
            "delivered": False,
        }
        with self._lock:
            self._actions.append(payload)
            self._actions = self._actions[-MAX_ACTIONS:]
        return payload

    def switch_tab(self, query: str) -> dict[str, Any]:
        tab = self.find_tab(query)
        if tab is None:
            return {"ok": False, "error": f"I couldn't find an open tab matching {query!r}."}
        action = self.queue_action("switch_tab", tab=tab)
        self._publish_action("switch_to_tab", {"query": query}, "running")
        return {
            "ok": True,
            "tab": tab,
            "action_id": action["id"],
            "queued": not self._extension_fresh(),
            "reply": f"I've switched to the {tab['title']} tab.",
        }

    def interact(
        self,
        tab_query: str,
        action: str,
        target: str = "",
        text: str = "",
    ) -> dict[str, Any]:
        tab = self.find_tab(tab_query)
        if tab is None:
            return {"ok": False, "error": f"I couldn't find an open tab matching {tab_query!r}."}
        kind = (action or "").strip().lower()
        if kind == "read":
            return self.read_tab(tab_query)
        if kind in ("click", "click_dom"):
            queued = self.queue_action("click_dom", tab=tab, target=target or tab_query)
            return {"ok": True, "tab": tab, "action_id": queued["id"], "action": "click",
                    "target": target or tab_query}
        if kind in ("type", "send", "type_dom"):
            submit = kind == "send"
            queued = self.queue_action(
                "type_dom", tab=tab, target=target, text=text, submit=submit
            )
            return {"ok": True, "tab": tab, "action_id": queued["id"], "action": "type",
                    "text": text, "submitted": submit}
        if kind in ("scroll", "scroll_dom"):
            queued = self.queue_action("scroll_dom", tab=tab, target=target, text=text)
            return {"ok": True, "tab": tab, "action_id": queued["id"], "action": "scroll"}
        return {"ok": False, "error": f"Unknown browser action {action!r}."}

    def record_result(self, payload: dict[str, Any]) -> None:
        action_id = str(payload.get("id") or "")
        if action_id:
            with self._lock:
                self._results[action_id] = dict(payload)

    # -- state -------------------------------------------------------------------

    def _extension_fresh(self) -> bool:
        with self._lock:
            return bool(self._last_sync and time.time() - self._last_sync < 5.0)

    @property
    def connected(self) -> bool:
        """True when the Brave/Firefox extension pushed a snapshot in the last 5 s."""
        return self._extension_fresh()

    def _publish(self, tabs: Iterable[dict[str, Any]]) -> None:
        state = get_state()
        try:
            state.set_browser_tabs(list(tabs))
            state.update_screen(browser_tabs=list(tabs))
        except Exception:
            pass

    def _publish_action(self, tool: str, args: dict[str, Any], status: str) -> None:
        try:
            get_state().set_action(tool, args, status)
        except Exception:
            pass

    def reset(self) -> None:
        with self._lock:
            self._tabs.clear()
            self._dom.clear()
            self._actions.clear()
            self._results.clear()
            self._last_sync = 0.0
            self._last_browser = ""


_hub = BrowserBridgeHub()


def get_browser_bridge() -> BrowserBridgeHub:
    return _hub
