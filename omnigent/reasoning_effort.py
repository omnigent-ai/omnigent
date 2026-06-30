"""Reasoning-effort validation helpers shared across client/runtime paths."""

from __future__ import annotations

from collections.abc import Iterable

from omnigent.llms.errors import PermanentLLMError

EFFORT_VALUES = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
EFFORT_CLEAR_VALUES = frozenset({"default", "off", "reset"})

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

# xAI / Grok accepts the OpenAI-compatible ``reasoning_effort`` parameter on only
# a subset of models. Sending it to the others (e.g. ``grok-4``,
# ``grok-code-fast-1``, ``grok-4-fast-reasoning``) is rejected with HTTP 400.
# Gate on this allow-set of model-id prefixes; unknown grok ids default to NOT
# sending the parameter. Refs: docs.x.ai reasoning docs (``reasoning_effort``
# "Only supported by grok-4.3"; ``grok-4.20-multi-agent`` uses ``reasoning.effort``
# for agent count; ``grok-3-mini`` historically supported it).
XAI_REASONING_EFFORT_MODEL_PREFIXES = (
    "grok-3-mini",
    "grok-4.3",
    "grok-4.20-multi-agent",
)


def provider_accepts_reasoning_effort(provider: str, model: str) -> bool:
    """Return whether *provider*/*model* accepts the ``reasoning_effort`` param.

    Only xAI restricts this per-model today; every other Chat Completions
    provider is treated as accepting it, preserving existing behavior. xAI
    rejects the parameter with HTTP 400 on models outside
    :data:`XAI_REASONING_EFFORT_MODEL_PREFIXES`.

    :param provider: The routed provider id, e.g. ``"xai"``.
    :param model: The model name without provider prefix, e.g. ``"grok-4"``.
    :returns: ``True`` if ``reasoning_effort`` may be sent for this model.
    """
    if provider == "xai":
        normalized = model.lower()
        return any(normalized.startswith(prefix) for prefix in XAI_REASONING_EFFORT_MODEL_PREFIXES)
    return True


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
    """Validate *effort* against *supported*, returning a string or None."""
    if effort is None or effort == "":
        return None
    effort_str = str(effort)
    if effort_str not in set(supported):
        raise ValueError(unsupported_effort_message(effort_str, provider, supported))
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
