"""Ultron — Argon 4 Fedora 45 Personal AI Assistant."""

from __future__ import annotations

import platform

__version__ = "0.1.0"


def os_release_name() -> str:
    """Return e.g. ``Fedora Linux 45`` so banners never go stale on an OS upgrade."""
    try:
        info = platform.freedesktop_os_release()
    except OSError:
        return "Linux"
    return f"{info.get('NAME', 'Linux')} {info.get('VERSION_ID', '')}".strip()
