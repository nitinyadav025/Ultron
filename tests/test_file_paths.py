"""Voice-style folder names must resolve against $HOME, not the project directory.

Also guards the live list_files bug: the model passed pattern="*down*" for a garbled
transcript, the folder was reported as empty although it held 43 items.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from google.genai import types

from assistant.brain.agent_loop import AgentLoop
from assistant.brain.gemini_client import TokenUsageStats
from assistant.brain.local_client import LocalUsageMeter
from assistant.brain.tool_replies import FOLDER_NOT_FOUND_REPLY, list_files_reply
from assistant.config import load_config
from assistant.safety.killswitch import WatchdogMonitor
from assistant.safety.permissions import PermissionGate
from assistant.tools.files import list_files, resolve_user_directory
from assistant.tools.registry import ToolRegistry

BAD_TRANSCRIPT = "List files, see in my down download for 4-fold editor"


def test_bare_folder_names_resolve_against_home() -> None:
    home = Path.home()
    assert resolve_user_directory("Downloads") == home / "Downloads"
    assert resolve_user_directory("my Downloads folder") == home / "Downloads"
    assert resolve_user_directory("the Documents directory") == home / "Documents"
    assert resolve_user_directory("Desktop") == home / "Desktop"


def test_spoken_singular_and_leading_words_are_robust() -> None:
    home = Path.home()
    # Whisper drops the plural ("download") and keeps filler words ("in my ...").
    assert resolve_user_directory("download") == home / "Downloads"
    assert resolve_user_directory("my download folder") == home / "Downloads"
    assert resolve_user_directory("in my downloads folder") == home / "Downloads"
    assert resolve_user_directory("documents") == home / "Documents"
    assert resolve_user_directory("desktop") == home / "Desktop"
    assert resolve_user_directory("home") == home


def test_absolute_and_relative_paths_are_left_alone(tmp_path: Path) -> None:
    assert resolve_user_directory(str(tmp_path)) == tmp_path
    assert resolve_user_directory(".") == Path(".")


def test_list_files_accepts_a_spoken_folder_name() -> None:
    result = list_files("my Downloads folder")
    assert result["ok"] is True
    assert result["directory"] == str((Path.home() / "Downloads").resolve())
    assert result["count"] >= 1


def test_a_model_invented_pattern_cannot_make_a_full_folder_look_empty(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("a", encoding="utf-8")
    (tmp_path / "photo.png").write_text("b", encoding="utf-8")

    filtered = list_files(str(tmp_path), pattern="*download*")

    assert filtered["ok"] is True
    assert filtered["count"] == 2  # the folder itself is listed, never "0 items"
    assert {item["name"] for item in filtered["items"]} == {"notes.txt", "photo.png"}


def test_a_really_empty_folder_still_reports_zero(tmp_path: Path) -> None:
    result = list_files(str(tmp_path), pattern="*download*")
    assert result["ok"] is True
    assert result["count"] == 0
    assert list_files_reply({"ok": True, "raw_summary": result}).startswith("There are no items")


def test_missing_folder_is_reported_as_not_found() -> None:
    result = list_files("definitely-not-a-real-folder-xyz")
    assert result["ok"] is False
    assert list_files_reply(result) == FOLDER_NOT_FOUND_REPLY


def test_spoken_folder_name_still_goes_through_the_safe_gate() -> None:
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(list_files)
    out = registry.execute("list_files", {"directory": "my Downloads folder"}, "list files")
    assert out["ok"] is True
    assert out["raw_summary"]["directory"] == str((Path.home() / "Downloads").resolve())


class ListFilesOnceAdapter:
    """Calls list_files with fixed arguments, then answers with the bad model sentence."""

    is_local = True
    usage_meter = LocalUsageMeter()

    def __init__(self, arguments: dict[str, Any]) -> None:
        self.arguments = arguments
        self.calls = 0

    def select_model(self, role: str = "main") -> str:
        return "fake-local"

    def generate_turn(
        self,
        contents: object,
        system_instruction: str,
        tools: list[dict[str, Any]] | None = None,
        role: str = "main",
        thinking_level: str | None = None,
        max_retries: int = 1,
        max_output_tokens: int | None = None,
    ) -> tuple[types.GenerateContentResponse, TokenUsageStats]:
        self.calls += 1
        if self.calls == 1:
            content = types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name="list_files", args=self.arguments, id="l1"))],
            )
        else:
            content = types.Content(
                role="model",
                parts=[types.Part.from_text(text="There are no items in your Downloads folder.")],
            )
        return types.GenerateContentResponse(candidates=[types.Candidate(content=content)]), TokenUsageStats()


def _loop_with(adapter: ListFilesOnceAdapter) -> AgentLoop:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(list_files)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    return AgentLoop(cfg, adapter, registry, watchdog)  # type: ignore[arg-type]


def test_live_garbled_transcript_lists_downloads_instead_of_claiming_empty() -> None:
    expected = list_files("~/Downloads")
    # The exact arguments the local model passed during the live bug.
    adapter = ListFilesOnceAdapter({"directory": "~/Downloads", "pattern": "*down*"})

    result = _loop_with(adapter).run_task(BAD_TRANSCRIPT)

    assert result.reply == f"There are {expected['count']} items in your Downloads folder."
    assert "no items" not in result.reply


def test_missing_folder_reply_never_claims_no_items() -> None:
    adapter = ListFilesOnceAdapter({"directory": "definitely-not-a-real-folder-xyz"})

    result = _loop_with(adapter).run_task("list files in definitely-not-a-real-folder-xyz")

    assert result.reply == FOLDER_NOT_FOUND_REPLY
    assert "no items" not in result.reply
    # B6: list_files has a deterministic reply (even on failure), so no second model turn.
    assert adapter.calls == 1


def test_model_empty_claim_without_a_tool_is_rejected() -> None:
    cfg = load_config()
    cfg.backend = "local"
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)
    watchdog = WatchdogMonitor(cfg.max_steps, cfg.wall_clock_timeout_sec, cfg.idle_standdown_sec)
    loop = AgentLoop(cfg, ListFilesOnceAdapter({}), registry, watchdog)  # type: ignore[arg-type]

    assert loop._finalize_reply("There are no items in your Downloads folder.", None) == FOLDER_NOT_FOUND_REPLY


def test_tool_calls_log_their_arguments_without_secrets(caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="ultron.tools")
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "yes")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(list_files)

    registry.execute("list_files", {"directory": str(Path.home() / "Downloads")})
    registry.execute("unknown_tool", {"api_key": "AIza" + "b" * 35})

    assert "Tool call: list_files" in caplog.text
    assert str(Path.home() / "Downloads") in caplog.text
    assert "AIza" not in caplog.text
