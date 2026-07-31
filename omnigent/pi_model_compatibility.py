"""Pi-specific model compatibility fallbacks absent from catalog metadata."""

from __future__ import annotations

# These system models omit the finish reason Pi requires on Chat Completions.
# ``glm-`` avoids matching vendor-direct ids without a system.ai alias.
SYSTEM_AI_RESPONSES_KEYWORDS: tuple[str, ...] = ("kimi", "inkling", "qwen3", "glm-")


def unsupported_in_pi(model_id_lower: str) -> bool:
    """Return whether Pi cannot parse the model's response content.

    These endpoints return typed-array content that Pi renders as
    ``[object Object]``; their available alternate wires do not fix it.
    """
    return "gemini-2-5" in model_id_lower or "gpt-oss" in model_id_lower
