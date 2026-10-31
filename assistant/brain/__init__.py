"""Brain adapters: Gemini (cloud) and local llama.cpp, plus the backend factory."""

from __future__ import annotations

from assistant.brain.gemini_client import GeminiAdapter
from assistant.brain.local_client import LocalAdapter
from assistant.config import AssistantConfig


def build_adapter(config: AssistantConfig) -> GeminiAdapter | LocalAdapter:
    """Return the configured brain adapter (BACKEND=local or gemini)."""
    if config.backend == "local":
        return LocalAdapter(config)
    return GeminiAdapter(config)
