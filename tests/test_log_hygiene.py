"""Tests never write into the real logs/ or workspace/ folders."""

from __future__ import annotations

from pathlib import Path

from assistant.config import LOGS_DIR, PROJECT_ROOT, WORKSPACE_DIR, load_config
from assistant.safety.permissions import PermissionGate
from assistant.tools.files import list_files
from assistant.tools.registry import ToolRegistry


def test_logs_and_workspace_point_at_a_temporary_folder() -> None:
    assert LOGS_DIR != PROJECT_ROOT / "logs"
    assert WORKSPACE_DIR != PROJECT_ROOT / "workspace"
    assert str(LOGS_DIR).startswith("/tmp/") or str(LOGS_DIR).startswith("/var/tmp/")
    assert str(WORKSPACE_DIR).startswith("/tmp/") or str(WORKSPACE_DIR).startswith("/var/tmp/")


def test_a_tool_call_is_logged_in_the_tmp_log_not_the_real_one(tmp_path: Path) -> None:
    real_log = PROJECT_ROOT / "logs" / "assistant.log"
    real_before = real_log.stat().st_mtime_ns if real_log.exists() else None

    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(list_files)
    registry.execute("list_files", {"directory": str(tmp_path)})

    real_after = real_log.stat().st_mtime_ns if real_log.exists() else None
    assert real_after == real_before
    assert "Tool call: list_files" in (LOGS_DIR / "assistant.log").read_text(encoding="utf-8")
    assert (LOGS_DIR / "audit.jsonl").exists()
