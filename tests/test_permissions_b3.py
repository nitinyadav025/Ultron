"""Part 1b proof: B2's permission changes did not change chat, and hands-free never asks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from assistant.config import load_config
from assistant.safety.permissions import PermissionGate
from assistant.tools.registry import ToolRegistry, tool


def _registry(tmp_path: Path, prompts: list[str], answer: str, *, allow_prompts: bool) -> tuple[ToolRegistry, list[str]]:
    executed: list[str] = []

    @tool(risk_level="confirm", read_only=False)
    def delete_note(path: str) -> dict[str, Any]:
        """Delete a note (confirm tier)."""
        executed.append(path)
        return {"ok": True, "path": path}

    cfg = load_config()

    def confirm(detail: str, _timeout: int) -> str:
        prompts.append(detail)
        return answer

    gate = PermissionGate(config=cfg, mode="ask", confirm_callback=confirm)
    registry = ToolRegistry(config=cfg, gate=gate, allow_prompts=allow_prompts)
    registry.register(delete_note)
    return registry, executed


def test_chat_still_prompts_and_runs_only_after_yes(tmp_path: Path) -> None:
    prompts: list[str] = []
    registry, executed = _registry(tmp_path, prompts, "yes", allow_prompts=True)

    result = registry.execute("delete_note", {"path": str(tmp_path / "note.txt")})

    assert prompts, "chat must still show the confirmation prompt"
    assert result["ok"] is True
    assert executed == [str(tmp_path / "note.txt")]


def test_chat_prompt_no_means_no_action(tmp_path: Path) -> None:
    prompts: list[str] = []
    registry, executed = _registry(tmp_path, prompts, "no", allow_prompts=True)

    result = registry.execute("delete_note", {"path": str(tmp_path / "note.txt")})

    assert prompts
    assert result["ok"] is False
    assert result.get("needs_confirmation") is None
    assert executed == []


def test_hands_free_never_prompts_and_never_runs(tmp_path: Path) -> None:
    prompts: list[str] = []
    registry, executed = _registry(tmp_path, prompts, "yes", allow_prompts=False)

    result = registry.execute("delete_note", {"path": str(tmp_path / "note.txt")})

    assert prompts == []  # the keyboard was never read
    assert result["ok"] is False
    assert result["needs_confirmation"] is True
    assert registry.needs_confirmation_seen is True
    assert executed == []
