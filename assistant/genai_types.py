"""Lazy access to ``google.genai.types``.

``BACKEND=local`` must never import the cloud SDK. Tool schemas use the neutral
``ToolDeclaration`` in ``assistant.tools.registry``; the few places that still build real
google-genai objects get them through this proxy, which imports the SDK on first attribute
access instead of at module import time.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


class _LazyTypes:
    def __getattr__(self, name: str) -> Any:
        return getattr(import_module("google.genai.types"), name)


types = _LazyTypes()
