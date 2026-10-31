"""Pytest suite for PermissionGate, ToolRegistry, Secret Redactor, and Sandbox Caps."""

from __future__ import annotations

from assistant.config import load_config
from assistant.safety.permissions import PermissionGate
from assistant.safety.redact import redact_secrets, run_redactor_self_test
from assistant.tools.files import list_files, read_file
from assistant.tools.registry import ToolRegistry
from assistant.tools.sandbox import run_sandboxed_script


def test_redactor_catches_planted_keys() -> None:
    ok, _ = run_redactor_self_test()
    assert ok is True
    assert "AIza" not in redact_secrets("key=AIzaSyD9876543210abcdefghijkLMNOPQRSTUV")


def test_permission_gate_blocks_env_and_system_delete() -> None:
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    assert not gate.check("read_file", "safe", True, {"path": ".env"}).allowed
    assert not gate.check("delete_file", "confirm", False, {"path": "/etc/passwd"}).allowed
    assert not gate.check("shell", "confirm", False, {"cmd": "setenforce 0"}).allowed


def test_model_and_thinking_switcher() -> None:
    cfg = load_config()
    cfg.backend = "gemini"
    assert cfg.set_thinking_level("high") == "high"
    assert cfg.set_active_model("gemini-3.5-flash-lite") == "gemini-3.5-flash-lite"

    local_cfg = load_config()
    local_cfg.backend = "local"
    assert local_cfg.current_model == "LFM3-1.7B-Q6_K"
    assert local_cfg.available_model_choices() == ["LFM3-1.7B-Q6_K"]
    try:
        local_cfg.set_active_model("anything")
    except ValueError as exc:
        assert "one fixed llama.cpp model" in str(exc)
    else:
        raise AssertionError("expected ValueError for model switching on local backend")


def test_tool_registry_schema_generation() -> None:
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe")
    reg = ToolRegistry(config=cfg, gate=gate)
    spec = reg.register(list_files)
    reg.register(read_file)
    assert spec.name == "list_files"
    assert len(reg.get_gemini_tools()) == 1

    openai_tools = reg.get_openai_tools()
    assert len(openai_tools) == 2
    first = openai_tools[0]
    assert first["type"] == "function"
    assert first["function"]["name"] == "list_files"
    assert first["function"]["parameters"]["type"] == "object"
    assert "directory" in first["function"]["parameters"]["properties"]
    assert first["function"]["parameters"]["properties"]["recursive"]["type"] == "boolean"


def test_sandbox_kills_runaway_sleep() -> None:
    res = run_sandboxed_script("import time\ntime.sleep(10)\n", filename="pytest_sleep.py", timeout_sec=2)
    assert res["killed_by_timeout"] is True
