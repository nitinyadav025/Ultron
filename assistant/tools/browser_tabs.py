"""Browser tab tools: list, switch and interact with Brave/Firefox tabs.

The primary path is the local ``ultron-browser-bridge`` extension. When it is not loaded,
every tool falls back to real desktop actions (wmctrl window focus, AT-SPI visible-element
clicks, ``wl-copy`` + ``ydotool`` typing) so the command still does something honest.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from assistant.environment.browser_bridge import get_browser_bridge
from assistant.tools.registry import tool

logger = logging.getLogger("ultron.tools.browser_tabs")


def _wmctrl_focus(title: str) -> bool:
    if not shutil.which("wmctrl"):
        return False
    try:
        done = subprocess.run(
            ["wmctrl", "-a", title],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@tool(risk_level="safe", read_only=True)
def list_browser_tabs() -> dict[str, Any]:
    """List every open Brave/Firefox tab (extension + CDP + window sensors)."""
    hub = get_browser_bridge()
    tabs = hub.list_tabs()
    return {
        "ok": True,
        "count": len(tabs),
        "tabs": tabs,
        "bridge_connected": hub.connected,
    }


@tool(risk_level="safe", read_only=False)
def switch_to_tab(query: str) -> dict[str, Any]:
    """Switch to any open browser tab matching a title, site or URL fragment."""
    hub = get_browser_bridge()
    if hub.connected:
        result = hub.switch_tab(query)
        if result.get("ok"):
            return result
    else:
        result = {"ok": False, "error": f"The browser bridge is not loaded; trying desktop focus for {query!r}."}
    # Fallback 1: a native XWayland window title can still be focused with wmctrl.
    if _wmctrl_focus(query):
        return {"ok": True, "method": "wmctrl", "reply": f"I've focused {query}."}
    # Fallback 2: click the visible tab/button through AT-SPI.
    try:
        from assistant.tools.screen_actions import click_element

        clicked = click_element(query)
        if clicked.get("ok"):
            return {"ok": True, "method": "atspi", "result": clicked,
                    "reply": f"I've switched to {query}."}
    except Exception as exc:
        logger.warning("AT-SPI tab switch failed: %s", type(exc).__name__)
    return result


@tool(risk_level="safe", read_only=False)
def interact_with_tab(
    tab_query: str,
    action: str,
    target: str = "",
    text: str = "",
) -> dict[str, Any]:
    """Read, click, type or send inside an open browser tab (WhatsApp, Gemini, Gmail…)."""
    hub = get_browser_bridge()
    kind = (action or "").strip().lower()
    result = (
        hub.interact(tab_query, kind, target=target, text=text)
        if hub.connected
        else {"ok": False, "error": "The browser bridge is not loaded; using desktop actions."}
    )

    if kind == "read" and not result.get("ok"):
        # No extension DOM: read the focused/named window through AT-SPI.
        try:
            from assistant.tools.screen_actions import read_screen

            fallback = read_screen(tab_query)
            if fallback.get("ok"):
                return {**fallback, "method": "atspi"}
        except Exception as exc:
            logger.warning("AT-SPI tab read failed: %s", type(exc).__name__)
        return result

    if result.get("ok"):
        return result

    # Extension absent or tab unknown: act on the real desktop.
    try:
        from assistant.tools.screen_actions import click_element, focus_and_type

        if kind == "click":
            fallback = click_element(target or tab_query)
            return {**fallback, "method": "atspi", "tab_query": tab_query}
        if kind in ("type", "send"):
            fallback = focus_and_type(target or "message box", text)
            if fallback.get("ok") and kind == "send":
                from assistant.tools.desktop_control import press_key

                press_key("enter")
            return {**fallback, "method": "atspi", "tab_query": tab_query,
                    "submitted": kind == "send"}
    except Exception as exc:
        logger.warning("AT-SPI tab fallback failed: %s", type(exc).__name__)
    return result
