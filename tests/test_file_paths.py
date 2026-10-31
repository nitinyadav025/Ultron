"""Voice-style folder names must resolve against $HOME, not the project directory."""

from __future__ import annotations

from pathlib import Path

from assistant.config import load_config
from assistant.safety.permissions import PermissionGate
from assistant.tools.files import list_files, resolve_user_directory
from assistant.tools.registry import ToolRegistry


def test_bare_folder_names_resolve_against_home() -> None:
    home = Path.home()
    assert resolve_user_directory("Downloads") == home / "Downloads"
    assert resolve_user_directory("my Downloads folder") == home / "Downloads"
    assert resolve_user_directory("the Documents directory") == home / "Documents"
    assert resolve_user_directory("Desktop") == home / "Desktop"


def test_absolute_and_relative_paths_are_left_alone(tmp_path: Path) -> None:
    assert resolve_user_directory(str(tmp_path)) == tmp_path
    assert resolve_user_directory(".") == Path(".")


def test_list_files_accepts_a_spoken_folder_name() -> None:
    result = list_files("my Downloads folder")
    assert result["ok"] is True
    assert result["directory"] == str((Path.home() / "Downloads").resolve())
    assert result["count"] >= 1


def test_spoken_folder_name_still_goes_through_the_safe_gate() -> None:
    cfg = load_config()
    gate = PermissionGate(config=cfg, mode="auto-safe", confirm_callback=lambda _d, _t: "no")
    registry = ToolRegistry(config=cfg, gate=gate)
    registry.register(list_files)
    out = registry.execute("list_files", {"directory": "my Downloads folder"}, "list files")
    assert out["ok"] is True
    assert out["raw_summary"]["directory"] == str((Path.home() / "Downloads").resolve())
