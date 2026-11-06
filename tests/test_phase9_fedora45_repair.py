"""Phase 9 — Fedora 45 / ULTRON repair regressions.

Covers the three live failures from the Fedora 45 upgrade:
1. ``BACKEND=local`` must never import google-genai (even with no SDK installed).
2. ``"hermes"`` is a real desktop app and must never resolve to Lutris.
3. A long spoken paragraph is not cut off (60 s cap, 2.6 s silence, 1.8 s early check).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from assistant.brain.fast_path import match_fast_command, try_fast_path
from assistant.config import AssistantConfig, load_config
from assistant.speech.recorder import DEFAULT_MAX_SECONDS
from assistant.speech.wake import normalise_spoken_command
from assistant.tools.apps import app_key, find_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )


# -- 1. lazy google-genai import -----------------------------------------------------------


def test_main_brain_and_gemini_client_import_without_google() -> None:
    result = _run_isolated(
        "import sys\n"
        "sys.modules['google'] = None\n"
        "import main\n"
        "import assistant.brain\n"
        "import assistant.brain.gemini_client\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_local_tool_registry_builds_schemas_without_google() -> None:
    result = _run_isolated(
        "import sys\n"
        "sys.modules['google'] = None\n"
        "from assistant.config import AssistantConfig\n"
        "from assistant.safety.permissions import PermissionGate\n"
        "from assistant.tools.registry import ToolRegistry, tool\n"
        "\n"
        "@tool(name='ping', risk_level='safe', read_only=True)\n"
        "def ping(target: str) -> dict:\n"
        "    '''Ping a target.'''\n"
        "    return {'ok': True, 'target': target}\n"
        "\n"
        "cfg = AssistantConfig()\n"
        "reg = ToolRegistry(cfg, PermissionGate(config=cfg, mode='auto-safe'))\n"
        "reg.register(ping)\n"
        "schemas = reg.get_openai_tools()\n"
        "assert schemas[0]['function']['name'] == 'ping'\n"
        "assert schemas[0]['function']['parameters']['required'] == ['target']\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_gemini_schema_still_builds_when_the_sdk_is_installed() -> None:
    from assistant.safety.permissions import PermissionGate
    from assistant.tools.registry import ToolRegistry, tool

    @tool(name="ping", risk_level="safe", read_only=True)
    def ping(target: str, count: int = 1) -> dict:
        """Ping a target."""
        return {"ok": True, "target": target, "count": count}

    cfg = AssistantConfig()
    registry = ToolRegistry(cfg, PermissionGate(config=cfg, mode="auto-safe"))
    registry.register(ping)

    tools = registry.get_gemini_tools()
    declaration = tools[0].function_declarations[0]
    assert declaration.name == "ping"
    assert declaration.parameters.required == ["target"]
    assert declaration.parameters.properties["count"].type.name == "INTEGER"


# -- 2. Hermes is its own app --------------------------------------------------------------


def test_hermes_resolves_to_the_real_desktop_app_and_never_lutris() -> None:
    assert app_key("hermes") == "hermes"
    match = find_app("hermes")
    assert match is not None
    assert match.key == "hermes"
    assert match.display == "Hermes"
    assert match.desktop_id == "com.nousresearch.hermes.desktop"


def test_open_hermes_never_becomes_open_lutris() -> None:
    assert normalise_spoken_command("open hermes") == "open hermes"
    command = match_fast_command("open hermes")
    assert command is not None
    assert command["tool"] == "open_app"
    assert command["arguments"] == {"app_name": "hermes"}

    lutris = match_fast_command("open loot risk")
    assert lutris is not None
    assert lutris["arguments"] == {"app_name": "lutris"}


# -- 3. long-paragraph listening -----------------------------------------------------------


def test_long_paragraph_listening_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("VOICE_SILENCE_MS", "VOICE_MAX_SECONDS", "LISTEN_EARLY_CHECK_MS"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config(env_path=tmp_path / "missing.env", settings_path=tmp_path / "missing.yaml")

    assert cfg.voice_max_seconds == 60.0
    assert cfg.voice_silence_ms == 2600
    assert cfg.listen_early_check_ms == 1800
    assert AssistantConfig().voice_max_seconds == 60.0
    assert DEFAULT_MAX_SECONDS == 60.0


def test_conversation_falls_through_to_the_llm() -> None:
    for text in ("hello", "hi", "what is your name", "who are you", "how are you", "thanks"):
        assert match_fast_command(text) is None, text
        assert try_fast_path(None, text) is None, text  # type: ignore[arg-type]
