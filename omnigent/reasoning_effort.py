"""Reasoning-effort validation helpers shared across client/runtime paths."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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


# Some models served through a harness reject the harness's full effort ladder.
# GLM on the codex/Responses wire accepts only up to ``high`` (no ``xhigh``), so
# a user default of ``xhigh``/``max`` 400s the turn. Map such a model to the
# effort to use instead of failing — GLM falls back to ``medium``.
#
# These are probed facts about ONE gateway's serving, not properties of the
# effort ladders themselves, so a deployment whose gateway caps other models
# overrides them via ``routing.effort_caps`` (see
# :class:`~omnigent.server.smart_routing.RoutingSettings`). The provider ladders
# above stay frozen: they are the wire APIs' own vocabularies.
_MODEL_EFFORT_FALLBACK: Mapping[str, str] = MappingProxyType({"glm-5-2": "medium"})
# Efforts a fallback model cannot accept, so a pinned high value coerces down.
_MODEL_EFFORT_UNSUPPORTED: Mapping[str, frozenset[str]] = MappingProxyType(
    {"glm-5-2": frozenset({"xhigh", "max"})}
)


@dataclass(frozen=True)
class ModelEffortCaps:
    """Per-model effort ceilings a deployment's gateway imposes.

    :param fallback: Bare model id → the effort to use when the requested one
        is barred. Also the effort a switch onto that model sends when the
        caller asked for none.
    :param unsupported: Bare model id → the efforts that model's backend
        rejects outright.
    """

    # default_factory, not default: a mapping is unhashable and dataclasses
    # rejects an unhashable default outright.
    fallback: Mapping[str, str] = field(default_factory=lambda: _MODEL_EFFORT_FALLBACK)
    unsupported: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: _MODEL_EFFORT_UNSUPPORTED
    )


#: The caps every deployment gets unless its ``routing:`` block overrides them.
DEFAULT_MODEL_EFFORT_CAPS = ModelEffortCaps()


def model_effort_caps(caps: ModelEffortCaps | None = None) -> ModelEffortCaps:
    """Resolve which effort caps apply, defaulting to this deployment's.

    ``None`` reads :class:`~omnigent.server.smart_routing.RoutingSettings` off
    the process caps, so a managed gateway that caps a different model set is
    honoured without every caller threading the value. Outside a server process
    (the runner holds no routing settings) that read yields the defaults, which
    are the frozen tables above — so runner-side clamping is unchanged.

    :param caps: Explicit caps, or ``None`` to read the deployment's.
    :returns: The caps to clamp with; never ``None``.
    """
    if caps is not None:
        return caps
    try:
        from omnigent.server.smart_routing import routing_settings
    except ImportError:  # pragma: no cover — a build without the server extra
        return DEFAULT_MODEL_EFFORT_CAPS
    return routing_settings().model_effort_caps or DEFAULT_MODEL_EFFORT_CAPS


def _bare_model(model: str) -> str:
    """Strip a catalog/gateway prefix and fold to the comparison spelling."""
    bare = model.rsplit("/", 1)[-1]
    for prefix in ("databricks-", "system.ai."):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
    return bare.replace(".", "-").lower()


def clamp_effort_for_model(
    effort: str | None,
    model: str | None,
    *,
    caps: ModelEffortCaps | None = None,
) -> str | None:
    """Coerce *effort* to one *model* accepts, keeping the user's pick otherwise.

    A model whose backend rejects a high effort (e.g. GLM has no ``xhigh``)
    falls back to a supported value rather than 400-ing the turn. Any other
    model, or an already-accepted effort, is returned unchanged.

    :param caps: Effort ceilings to clamp against; ``None`` uses this
        deployment's (see :func:`model_effort_caps`).
    """
    if effort is None or model is None:
        return effort
    resolved = model_effort_caps(caps)
    key = _bare_model(model)
    unsupported = resolved.unsupported.get(key)
    if unsupported is not None and effort in unsupported:
        return resolved.fallback.get(key, effort)
    return effort


def effort_for_model_switch(
    effort: str | None,
    model: str | None,
    *,
    caps: ModelEffortCaps | None = None,
) -> str | None:
    """Effort to send when switching to *model*, guarding a rejected default.

    Like :func:`clamp_effort_for_model` for an explicit *effort*. When *effort*
    is ``None`` (no effort requested), a model that caps its ladder still needs
    guarding, because the switched-to thread inherits the config's default
    (which may be too high): return that model's fallback so the live turn does
    not 400. A model with no cap and no requested effort returns ``None``.

    :param caps: Effort ceilings to clamp against; ``None`` uses this
        deployment's (see :func:`model_effort_caps`).
    """
    if effort is not None:
        return clamp_effort_for_model(effort, model, caps=caps)
    if model is None:
        return None
    return model_effort_caps(caps).fallback.get(_bare_model(model))


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
