"""Gate 2B — Liquid Glass V18 dashboard: static bundle served by the environment server.

Verifies that ``GET /`` serves the real dashboard from ``assistant/dashboard/dist/``
with the right content types, that every view and control required by the V18 spec is
present in the markup, and that ``app.js`` binds to every documented API endpoint.
No network beyond localhost; no real mic/speaker/GUI.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from assistant.config import PROJECT_ROOT, load_config
from assistant.environment.server import EnvironmentServer
from assistant.environment.state import EnvironmentState

DIST = PROJECT_ROOT / "assistant" / "dashboard" / "dist"

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def _brain(text: str, source: str = "api") -> dict[str, Any]:
    return {"reply": f"Echo: {text}", "tools_called": ["demo"], "llm_ms": 7.5}


@pytest.fixture()
def dashboard_server(tmp_path: Path):
    state = EnvironmentState(config_path=tmp_path / "env.json")
    state.set_status("listening")
    server = EnvironmentServer(load_config(), state=state, port=0, brain=_brain)
    url = server.start()
    yield server, url
    server.stop()


def _get(url: str, path: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url + path)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _post(url: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


# --------------------------------------------------------------------------------------
# The bundle itself
# --------------------------------------------------------------------------------------


def test_dashboard_bundle_exists_on_disk() -> None:
    for name in ("index.html", "styles.css", "app.js"):
        path = DIST / name
        assert path.is_file(), f"missing dashboard asset: {path}"
        assert path.stat().st_size > 1000, f"{name} looks empty"


def test_no_remote_asset_loads_in_bundle() -> None:
    """Offline purity: the bundle loads no CDN scripts, remote fonts or remote URLs."""
    html = (DIST / "index.html").read_text(encoding="utf-8")
    css = (DIST / "styles.css").read_text(encoding="utf-8")
    js = (DIST / "app.js").read_text(encoding="utf-8")
    # only local asset references
    assert '<script src="app.js">' in html
    assert '<link rel="stylesheet" href="styles.css">' in html
    for text in (html, css, js):
        for pattern in (
            '<script src="http', '<link href="http', "@import", "url(http",
            "fonts.googleapis", "cdn.jsdelivr", "unpkg.com",
        ):
            assert pattern not in text, f"remote asset load detected: {pattern}"
    assert "http" not in css  # styles carry no URLs at all


def test_voice_modes_declared_in_markup() -> None:
    html = (DIST / "index.html").read_text(encoding="utf-8")
    js = (DIST / "app.js").read_text(encoding="utf-8")
    for mode in ("wake_word", "continuous", "ptt"):
        assert f'data-mode="{mode}"' in html
    assert "dataset.mode" in js and "voice_mode" in js


# --------------------------------------------------------------------------------------
# Serving through the real environment server
# --------------------------------------------------------------------------------------


def test_index_served_with_all_views(dashboard_server) -> None:
    server, url = dashboard_server
    assert server.dashboard_dir == DIST, "server must prefer assistant/dashboard/dist/"
    status, content_type, body = _get(url, "/")
    assert status == 200
    assert content_type.startswith("text/html")
    html = body.decode("utf-8")
    assert "Ultron" in html
    # every V18 navigation destination
    for view in (
        "overview", "chat", "tabs", "apps", "skills", "telemetry", "settings", "bridge",
    ):
        assert f'id="view-{view}"' in html, f"missing view section: {view}"
    # rail, stage, HUD stack, visionOS grabber, waveform, spline, donut
    for marker in (
        'class="rail"', 'class="stage"', 'class="hud-stack"', "vision-grabber",
        "hud-waveform", "hud-spline-line", "ov-donut", "ov-ticks", "mode-select",
        "quick-search", "pill-llama", "badge-tabs", "badge-apps", "badge-skills",
    ):
        assert marker in html, f"missing markup marker: {marker}"


def test_styles_and_app_served_with_correct_mime(dashboard_server) -> None:
    _, url = dashboard_server
    status, content_type, body = _get(url, "/styles.css")
    assert status == 200
    assert content_type.startswith("text/css")
    css = body.decode("utf-8")
    for marker in (
        "grid-template-columns: var(--rail-width) minmax(640px, 1fr) var(--hud-width)",
        "rgba(58, 50, 44, 0.52)",           # frosted rail glass (demo)
        "blur(36px) saturate(160%)",         # rail backdrop
        "blur(44px) saturate(180%)",         # stage backdrop
        "rgba(68, 138, 255, 0.72)",          # sapphire HUD voice card
        "cubic-bezier(0.22, 1, 0.36, 1)",    # spatial spring
        "Lora", "Inter",                     # demo typography stacks
        "vision-grabber", "grabber-bar", "glass-card", "pure-glass",
    ):
        assert marker in css, f"styles.css missing: {marker}"

    status, content_type, body = _get(url, "/app.js")
    assert status == 200
    assert content_type.split(";")[0].strip() in ("application/javascript", "text/javascript")
    js = body.decode("utf-8")
    # every documented endpoint the dashboard must bind to
    for endpoint in (
        "/api/state", "/api/stream", "/api/command", "/api/voice/control",
        "/api/config", "/api/apps", "/api/apps/action", "/api/tabs", "/api/tabs/action",
        "/api/screen/action", "/api/skills",
    ):
        assert endpoint in js, f"app.js missing API binding: {endpoint}"
    # key behaviours
    assert 'addEventListener("state"' in js, "SSE state event binding missing"
    assert "EventSource" in js
    assert "barge_in" in js
    assert "ptt_start" in js and "ptt_stop" in js


def test_static_fallback_and_traversal_rejected(dashboard_server) -> None:
    _, url = dashboard_server
    status, _, body = _get(url, "/nonexistent-page")
    assert status == 200  # SPA fallback serves index.html
    assert b"view-overview" in body
    status, _, body = _get(url, "/../assistant/environment/server.py")
    assert status == 404, "path traversal must be rejected"
    assert b"dispatch" not in body


def test_hud_column_is_never_an_overlay() -> None:
    """The 3-widget stack must be a grid column, never a fixed/absolute overlay."""
    css = (DIST / "styles.css").read_text(encoding="utf-8")
    start = css.index(".hud-stack {")
    block = css[start : css.index("}", start)]
    assert "position: fixed" not in block
    assert "position: absolute" not in block
    assert "grid-column: 3" in block
    html = (DIST / "index.html").read_text(encoding="utf-8")
    assert 'class="spatial-shell"' in html
    assert 'class="rail"' in html and 'class="stage"' in html and 'class="hud-stack"' in html


def test_launcher_opens_a_widescreen_window() -> None:
    source = (PROJECT_ROOT / "assistant" / "environment" / "launcher.py").read_text(encoding="utf-8")
    assert "--window-size=1480,860" in source
    assert "--window-position=120,80" in source


def test_dashboard_interacts_with_live_api(dashboard_server) -> None:
    """The same server instance must keep serving the API the dashboard calls."""
    _, url = dashboard_server
    # mode-only POST (no action key) is the dashboard's mode-switch shape
    status, payload = _post(url, "/api/voice/control", {"mode": "continuous"})
    assert status == 200 and payload["ok"] is True
    assert payload["voice_mode"] == "continuous"
    status, payload = _post(url, "/api/voice/control", {"mode": "wake_word"})
    assert status == 200 and payload["voice_mode"] == "wake_word"
    status, payload = _post(url, "/api/command", {"text": "hello dashboard", "speak": False})
    assert status == 200 and payload["ok"] is True
    assert "Echo: hello dashboard" in payload["reply"]
    status, payload = _post(url, "/api/screen/action", {"action": "click", "ref": "@e1"})
    assert status in (200, 500)  # handled route; 500 only if AT-SPI is unavailable here
