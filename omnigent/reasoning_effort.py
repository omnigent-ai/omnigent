"""Reasoning-effort validation helpers shared across client/runtime paths."""

from __future__ import annotations

from collections.abc import Iterable

from omnigent.llms.errors import PermanentLLMError

EFFORT_VALUES = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
EFFORT_CLEAR_VALUES = frozenset({"default", "off", "reset"})

# Deprecated / vendor-written effort values mapped to the canonical value to
# use instead. The ChatGPT desktop app writes ``model_reasoning_effort =
# "ultra"`` into ``~/.codex/config.toml``, and the codex CLI forwards it as
# the retired ``max`` wire value — the OpenAI Responses API accepts neither
# (its ladder tops out at ``xhigh``). ``validate_effort`` coerces an alias
# only when the raw value is unsupported but the canonical value IS
# supported, so providers that genuinely support ``max`` (Anthropic) keep it
# unchanged.
EFFORT_ALIASES: dict[str, str] = {"ultra": "xhigh", "max": "xhigh"}

OPENAI_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
ANTHROPIC_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_EFFORTS = ANTHROPIC_EFFORTS
CODEX_EFFORTS = OPENAI_EFFORTS
OPENAI_AGENTS_EFFORTS = OPENAI_EFFORTS
GEMINI_EFFORTS = frozenset({"low", "medium", "high"})
ANTIGRAVITY_EFFORTS = GEMINI_EFFORTS
# The GitHub Copilot SDK's ``create_session(reasoning_effort=...)`` accepts
# exactly these levels (``copilot.session.ReasoningEffort`` literal); per-model
# support is gated by the Copilot backend (``list_models()``).
COPILOT_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


def format_supported(values: Iterable[str]) -> str:
    """Return a stable comma-separated supported-values string."""
    order = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    values_set = set(values)
    return ", ".join(value for value in order if value in values_set)


def unsupported_effort_message(effort: str, provider: str, supported: Iterable[str]) -> str:
    """Build a clear unsupported-effort error message."""
    return (
        f"Effort {effort!r} is not supported by {provider}; "
        f"supported values: {format_supported(supported)}"
    )


def validate_effort(effort: object, provider: str, supported: Iterable[str]) -> str | None:
    """Validate *effort* against *supported*, returning a string or None.

    A deprecated alias (see :data:`EFFORT_ALIASES`) is coerced to its
    canonical value when the raw value is unsupported but the canonical one
    is — e.g. the ChatGPT app's ``ultra`` becomes ``xhigh`` for codex, while
    ``max`` stays ``max`` for providers that still support it (Anthropic).
    """
    if effort is None or effort == "":
        return None
    effort_str = str(effort)
    supported_set = set(supported)
    if effort_str not in supported_set:
        alias = EFFORT_ALIASES.get(effort_str)
        if alias is not None and alias in supported_set:
            return alias
        raise ValueError(unsupported_effort_message(effort_str, provider, supported_set))
    return effort_str


def validate_effort_or_llm_error(
    effort: object,
    provider: str,
    supported: Iterable[str],
) -> str | None:
    """Validate for native LLM paths, raising non-retryable PermanentLLMError."""
    try:
        return validate_effort(effort, provider, supported)
    except ValueError as exc:
        raise PermanentLLMError(str(exc), code="unsupported_reasoning_effort") from exc
