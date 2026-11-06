"""Brain adapters: Gemini (cloud) and local llama.cpp, plus the backend factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from assistant.config import AssistantConfig

if TYPE_CHECKING:
    from assistant.brain.gemini_client import GeminiAdapter
    from assistant.brain.local_client import LocalAdapter


def build_adapter(config: AssistantConfig) -> GeminiAdapter | LocalAdapter:
    """Return the configured brain adapter (BACKEND=local or gemini).

    Both adapters are imported lazily so ``BACKEND=local`` never loads google-genai.
    """
    if config.backend == "local":
        from assistant.brain.local_client import LocalAdapter

        return LocalAdapter(config)
    from assistant.brain.gemini_client import GeminiAdapter

    return GeminiAdapter(config)
