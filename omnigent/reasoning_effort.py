"""Reasoning-effort validation helpers shared across client/runtime paths."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from omnigent.llms.errors import PermanentLLMError

EFFORT_VALUES = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
EFFORT_CLEAR_VALUES = frozenset({"default", "off", "reset"})

# Fold a value to a canonical one, but only where the target ladder lacks it:
# ``validate_effort`` applies an alias only when the raw value is unsupported
# but the canonical one is. On the SDK/Responses codex ladder (``CODEX_EFFORTS``,
# capped at ``xhigh``) the ChatGPT app's ``ultra`` / retired ``max`` fold to
# ``xhigh``; ladders that carry them (codex-native ``CODEX_NATIVE_EFFORTS``,
# Anthropic's ``max``) keep them unchanged.
EFFORT_ALIASES: dict[str, str] = {"ultra": "xhigh", "max": "xhigh"}

OPENAI_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
ANTHROPIC_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_EFFORTS = ANTHROPIC_EFFORTS
CODEX_EFFORTS = OPENAI_EFFORTS
# Codex-native drives the real codex process, which is the per-model authority
# on reasoning levels — it advertises them via ``model/list`` and validates the
# pairing itself. Sol reaches ``ultra``; the picker already gates which levels a
# model offers, so accept codex's full ladder here rather than re-clamping a
# valid pick down to ``xhigh``.
CODEX_NATIVE_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
OPENAI_AGENTS_EFFORTS = OPENAI_EFFORTS
GEMINI_EFFORTS = frozenset({"low", "medium", "high"})
ANTIGRAVITY_EFFORTS = GEMINI_EFFORTS
# The GitHub Copilot SDK's ``create_session(reasoning_effort=...)`` accepts
# exactly these levels (``copilot.session.ReasoningEffort`` literal); per-model
# support is gated by the Copilot backend (``list_models()``).
COPILOT_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
# pi's ``--thinking`` ladder is ``off|minimal|low|medium|high|xhigh|max``. Its
# ``off`` is omnigent's ``none`` — ``off`` is reserved here as a
# clear-to-default sentinel — so :func:`to_pi_thinking_level` translates.
PI_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})

#: pi's spelling of "no thinking", i.e. canonical ``none``.
PI_THINKING_OFF = "off"


def to_pi_thinking_level(effort: str) -> str:
    """Translate a canonical effort to pi's ``--thinking`` / RPC level.

    ``none`` maps to pi's ``off``; ``ultra`` (not in pi's ladder) clamps to
    ``max`` (the highest rung pi advertises); every other value is identical.

    :param effort: A canonical effort from :data:`PI_EFFORTS`.
    :returns: The ``ThinkingLevel`` string pi accepts.
    """
    if effort == "none":
        return PI_THINKING_OFF
    # PI_THINKING_LADDER is resolved at call time (defined later in this module).
    if effort not in PI_THINKING_LADDER:
        return PI_THINKING_LADDER[-1]  # clamp ultra → max (pi's highest rung)
    return effort


#: pi's ladder ascending, in pi's spelling. Index distance here is what
#: "nearest supported level" means.
PI_THINKING_LADDER: tuple[str, ...] = (
    PI_THINKING_OFF,
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def nearest_pi_thinking_level(level: str, available: Iterable[str]) -> str | None:
    """Clamp a pi thinking *level* to the nearest one the model supports.

    pi reports a per-model ladder via the ``get_available_thinking_levels``
    RPC; a model that doesn't offer the requested rung must not 400 the turn,
    so pick the closest offered rung instead. Ties resolve downward, spending
    fewer tokens than asked for rather than more.

    :param level: The requested level in pi's spelling (see
        :func:`to_pi_thinking_level`).
    :param available: Levels pi reports for the current model.
    :returns: The level to send, or ``None`` when *available* is empty (pi
        reported nothing usable, so the caller should leave the level alone).
    """
    offered = [value for value in PI_THINKING_LADDER if value in set(available)]
    if not offered:
        return None
    if level in offered:
        return level
    if level not in PI_THINKING_LADDER:
        return None
    target = PI_THINKING_LADDER.index(level)
    # Ascending order + stable min(): an equidistant pair resolves downward.
    return min(offered, key=lambda value: abs(PI_THINKING_LADDER.index(value) - target))


def efforts_for_harness(harness: str | None) -> frozenset[str] | None:
    """
    Return the effort vocabulary *harness* accepts.

    Three outcomes, and the difference between the last two matters:

    - a non-empty set — the harness takes an effort from that vocabulary;
    - an empty set — the harness is known and has no effort plumbing
      (``EffortFamily.NONE``), so any effort is meaningless to it;
    - ``None`` — the harness is not in the capability registry, so it
      cannot be classified. Callers that filter should pass the value
      through rather than drop it, since a plugin-registered harness may
      handle an effort this process knows nothing about.

    :param harness: Harness name or alias, e.g. ``"claude-native"``.
    :returns: Accepted values, an empty set, or ``None`` when unknown.
    """
    # Imported inside the function: harness_plugins pulls in a large slice
    # of the package, and a module-level edge from here has produced an
    # import cycle before.
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.harness_capabilities import EffortFamily
    from omnigent.harness_plugins import harness_capabilities

    if harness is None:
        return None
    canonical = canonicalize_harness(harness) or harness
    capabilities = harness_capabilities().get(canonical)
    if capabilities is None:
        return None
    # Every family that has a vocabulary must be listed: an unmapped family
    # falls through to the empty set, which callers read as "known harness with
    # no effort plumbing" and silently drop the effort. A new EffortFamily
    # therefore has to be added here in the same change that declares it.
    return {
        EffortFamily.ANTHROPIC: ANTHROPIC_EFFORTS,
        EffortFamily.OPENAI: OPENAI_EFFORTS,
        EffortFamily.GEMINI: GEMINI_EFFORTS,
        EffortFamily.COPILOT: COPILOT_EFFORTS,
        EffortFamily.PI: PI_EFFORTS,
    }.get(capabilities.effort, frozenset())


def format_supported(values: Iterable[str]) -> str:
    """Return a stable comma-separated supported-values string."""
    order = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
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
# GLM tops out at ``high``, so every rung above it (``xhigh``/``max``/``ultra``)
# is unsupported.
_MODEL_EFFORT_UNSUPPORTED: Mapping[str, frozenset[str]] = MappingProxyType(
    {"glm-5-2": frozenset({"xhigh", "max", "ultra"})}
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
