"""Desktop-native application launcher for Fedora 44 GNOME 50.5 Wayland."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from assistant.config import PROJECT_ROOT
from assistant.tools.registry import tool


def resolve_app_launch_command(app_name: str) -> tuple[list[str], str]:
    """Resolve a human app name (e.g. 'Text Editor') to its Fedora launch command."""
    cleaned = app_name.strip().lower()
    apps_file = PROJECT_ROOT / "config" / "apps.yaml"
    catalog = (yaml.safe_load(apps_file.read_text(encoding="utf-8")) or {}).get("apps", {}) if apps_file.exists() else {}

    entry = catalog.get(cleaned) or next((v for k, v in catalog.items() if cleaned in k or k in cleaned), None)
    if entry:
        for key in ("binary", "alt_binary"):
            b = entry.get(key, "")
            if b and shutil.which(b):
                return [b], b
        desktop_id = entry.get("desktop_id", "")
        if desktop_id and shutil.which("gtk-launch"):
            return ["gtk-launch", desktop_id], desktop_id
        flatpak_id = entry.get("flatpak_id", "")
        if flatpak_id and shutil.which("flatpak"):
            return ["flatpak", "run", flatpak_id], flatpak_id

    if cleaned.endswith(".desktop") and shutil.which("gtk-launch"):
        return ["gtk-launch", app_name.strip()], app_name.strip()
    if shutil.which(cleaned):
        return [cleaned], cleaned

    for ddir in (Path("/usr/share/applications"), Path("/var/lib/flatpak/exports/share/applications"), Path.home() / ".local/share/applications"):
        if ddir.exists():
            for dfile in ddir.glob("*.desktop"):
                if cleaned.replace(" ", "") in dfile.stem.lower().replace("-", ""):
                    if shutil.which("gtk-launch"):
                        return ["gtk-launch", dfile.name], dfile.name
                    if shutil.which("gio"):
                        return ["gio", "launch", str(dfile)], dfile.name
    return [cleaned], cleaned


@tool(risk_level="safe", read_only=False)
def open_app(app_name: str) -> dict[str, Any]:
    """Open a desktop app by name."""
    cmd, resolved = resolve_app_launch_command(app_name)
    if not shutil.which(cmd[0]):
        return {"ok": False, "app_name": app_name, "command": cmd, "error": f"Executable '{cmd[0]}' not found."}
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        try:
            _, err = proc.communicate(timeout=0.4)
            if proc.returncode not in (0, None):
                return {"ok": False, "app_name": app_name, "command": cmd, "error": err.decode("utf-8", errors="replace").strip()}
        except subprocess.TimeoutExpired:
            pass
        return {"ok": True, "app_name": app_name, "resolved": resolved, "command": cmd, "pid": proc.pid}
    except Exception as exc:
        return {"ok": False, "app_name": app_name, "command": cmd, "error": str(exc)}
