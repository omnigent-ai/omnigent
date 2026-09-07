"""Unit tests for deprecated reasoning-effort alias handling.

The ChatGPT desktop app writes ``model_reasoning_effort = "ultra"`` into
``~/.codex/config.toml``; the codex CLI forwards it as the retired ``max``
wire value. Neither is accepted by the OpenAI Responses API anymore, so
``validate_effort`` coerces those aliases to ``xhigh`` — but only for
providers whose supported set doesn't already contain the raw value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omnigent.reasoning_effort import (
    ANTHROPIC_EFFORTS,
    CODEX_EFFORTS,
    CODEX_NATIVE_EFFORTS,
    DEFAULT_MODEL_EFFORT_CAPS,
    EFFORT_VALUES,
    PI_EFFORTS,
    ModelEffortCaps,
    clamp_effort_for_model,
    effort_for_model_switch,
    model_effort_caps,
    nearest_pi_thinking_level,
    to_pi_thinking_level,
    validate_effort,
)


def test_ultra_coerces_to_xhigh_for_codex() -> None:
    """The ChatGPT-app ``ultra`` maps to ``xhigh`` on the codex ladder."""
    assert validate_effort("ultra", "codex", CODEX_EFFORTS) == "xhigh"


def test_max_coerces_to_xhigh_for_codex() -> None:
    """The retired ``max`` wire value maps to ``xhigh`` on the codex ladder."""
    assert validate_effort("max", "codex", CODEX_EFFORTS) == "xhigh"


def test_codex_native_accepts_max_and_ultra() -> None:
    """Codex-native honors the real per-model ladder — max/ultra pass through.

    The SDK/Responses codex ladder still caps at ``xhigh`` (see the tests
    above), but codex-native drives the real codex process, which accepts
    Sol's ``max``/``ultra``, so they must not fold to ``xhigh``.
    """
    assert validate_effort("ultra", "codex", CODEX_NATIVE_EFFORTS) == "ultra"
    assert validate_effort("max", "codex", CODEX_NATIVE_EFFORTS) == "max"


def test_ultra_preserved_in_session_metadata_vocabulary() -> None:
    """A terminal-observed ``ultra`` effort change is stored as ``ultra``.

    The codex-native forwarder mirrors the level the codex TUI reports; Sol's
    ``ultra`` is a real reasoning level, so the session metadata keeps it
    verbatim (the UI shows the true level) rather than folding it to ``xhigh``.
    """
    assert validate_effort("ultra", "session metadata", EFFORT_VALUES) == "ultra"


def test_max_stays_max_where_supported() -> None:
    """``max`` is NOT coerced for providers that genuinely support it."""
    assert validate_effort("max", "Claude Agent SDK", ANTHROPIC_EFFORTS) == "max"
    assert validate_effort("max", "session metadata", EFFORT_VALUES) == "max"


def test_unknown_effort_still_raises() -> None:
    """Values with no alias keep failing loud — no silent guessing."""
    with pytest.raises(ValueError, match="not supported"):
        validate_effort("turbo", "codex", CODEX_EFFORTS)


def test_supported_values_pass_through_unchanged() -> None:
    """In-vocabulary values are returned verbatim."""
    assert validate_effort("xhigh", "codex", CODEX_EFFORTS) == "xhigh"
    assert validate_effort("high", "codex", CODEX_EFFORTS) == "high"


def test_none_and_empty_clear_effort() -> None:
    """``None`` / empty string still mean "no explicit effort"."""
    assert validate_effort(None, "codex", CODEX_EFFORTS) is None
    assert validate_effort("", "codex", CODEX_EFFORTS) is None


# ── Per-model effort ceiling: GLM has no xhigh ──────────────────────────────
#
# GLM serves through the codex/Responses wire but rejects the top of the codex
# ladder: its only efforts are (disabled/none/minimal/low/medium/high). A user
# default of xhigh/max 400s the turn, so a routed GLM pick clamps down to
# medium. GLM appears in several catalog/gateway spellings and all must clamp.


@pytest.mark.parametrize(
    "model",
    ["glm-5-2", "databricks-glm-5-2", "system.ai.glm-5-2", "GLM-5-2", "system.ai.glm-5.2"],
)
@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
def test_glm_clamps_unsupported_effort_to_medium(model: str, effort: str) -> None:
    """Every GLM spelling coerces every above-high rung (xhigh/max/ultra) to medium."""
    assert clamp_effort_for_model(effort, model) == "medium"


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high"])
def test_glm_keeps_efforts_it_supports(effort: str) -> None:
    """GLM's own supported ladder passes through unchanged."""
    assert clamp_effort_for_model(effort, "system.ai.glm-5-2") == effort


@pytest.mark.parametrize("effort", ["xhigh", "max", "high", "medium", "low", None])
def test_non_glm_models_are_never_clamped(effort: str | None) -> None:
    """A model with no ceiling keeps whatever effort it was given, incl. xhigh."""
    for model in ("databricks-gpt-5-6-sol", "gpt-5-6-luna", "databricks-claude-opus-4-8"):
        assert clamp_effort_for_model(effort, model) == effort


def test_clamp_is_a_noop_without_a_model() -> None:
    """No model to key on ⇒ the effort is returned untouched."""
    assert clamp_effort_for_model("xhigh", None) == "xhigh"
    assert clamp_effort_for_model(None, "system.ai.glm-5-2") is None


def test_switch_to_glm_forces_medium_when_no_effort_requested() -> None:
    """Switching to GLM with no explicit effort still guards the live turn.

    Regression: a routed GLM turn sends ``thread/settings/update`` with a model
    but no effort, so the thread inherits config.toml's xhigh and 400s. The
    switch helper supplies GLM's fallback so the turn does not fail.
    """
    assert effort_for_model_switch(None, "system.ai.glm-5-2") == "medium"


def test_switch_to_glm_clamps_an_explicit_effort() -> None:
    """An explicit xhigh on a GLM switch still coerces to medium."""
    assert effort_for_model_switch("xhigh", "glm-5-2") == "medium"
    assert effort_for_model_switch("high", "glm-5-2") == "high"


def test_switch_to_uncapped_model_without_effort_stays_none() -> None:
    """A model with no ceiling and no requested effort sends no override."""
    assert effort_for_model_switch(None, "databricks-gpt-5-6-sol") is None
    assert effort_for_model_switch(None, None) is None


# ── Deployment-configurable effort caps ────────────────────────────────────
#
# The GLM ceiling above is one gateway's probed fact, not a property of the
# effort ladders. A deployment whose gateway caps a different model set puts it
# in ``routing.effort_caps`` instead of forking the module, so both clamps must
# read the caps rather than the frozen tables.


def _caps(fallback: dict[str, str], unsupported: dict[str, frozenset[str]]) -> ModelEffortCaps:
    return ModelEffortCaps(fallback=fallback, unsupported=unsupported)


def test_default_caps_are_the_frozen_tables() -> None:
    assert model_effort_caps(None).fallback["glm-5-2"] == "medium"
    assert model_effort_caps(DEFAULT_MODEL_EFFORT_CAPS) is DEFAULT_MODEL_EFFORT_CAPS


def test_explicit_caps_replace_the_glm_ceiling() -> None:
    caps = _caps({"gpt-5-6-sol": "low"}, {"gpt-5-6-sol": frozenset({"high", "xhigh"})})
    assert clamp_effort_for_model("xhigh", "databricks-gpt-5-6-sol", caps=caps) == "low"
    # GLM is uncapped under these caps, so its effort passes through.
    assert clamp_effort_for_model("xhigh", "system.ai.glm-5-2", caps=caps) == "xhigh"


def test_explicit_caps_drive_the_model_switch_default() -> None:
    caps = _caps({"gpt-5-6-sol": "low"}, {})
    assert effort_for_model_switch(None, "databricks-gpt-5-6-sol", caps=caps) == "low"
    assert effort_for_model_switch(None, "system.ai.glm-5-2", caps=caps) is None


def test_routing_settings_effort_caps_reach_the_clamp() -> None:
    """A ``routing.effort_caps`` override reaches the clamp with no threading."""
    from omnigent.server.smart_routing import RoutingSettings, parse_routing_tables

    settings = RoutingSettings(
        **parse_routing_tables(
            {"effort_caps": {"gpt-5.6-sol": {"fallback": "medium", "unsupported": ["xhigh"]}}}
        )
    )
    with patch("omnigent.runtime._globals._caps", new=SimpleNamespace(routing_settings=settings)):
        assert clamp_effort_for_model("xhigh", "databricks-gpt-5-6-sol") == "medium"
        # The configured table REPLACES the default, so GLM is no longer capped.
        assert clamp_effort_for_model("xhigh", "system.ai.glm-5-2") == "xhigh"
    # Outside that deployment the frozen default is back.
    assert clamp_effort_for_model("xhigh", "system.ai.glm-5-2") == "medium"


def test_pi_ladder_is_the_full_canonical_set() -> None:
    """pi accepts every canonical effort, so nothing is rejected at validation."""
    assert PI_EFFORTS == EFFORT_VALUES
    for effort in sorted(EFFORT_VALUES):
        assert validate_effort(effort, "pi", PI_EFFORTS) == effort
    with pytest.raises(ValueError):
        validate_effort("turbo", "pi", PI_EFFORTS)


def test_pi_thinking_level_translates_none_to_off() -> None:
    """Canonical ``none`` is pi's ``off``; the rest are spelled identically."""
    assert to_pi_thinking_level("none") == "off"
    for effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
        assert to_pi_thinking_level(effort) == effort
    # ``off`` stays a clear-to-default sentinel on the omnigent side, so it is
    # never handed to the translator — validation rejects it first.
    with pytest.raises(ValueError):
        validate_effort("off", "pi", PI_EFFORTS)


def test_pi_thinking_level_clamps_ultra_to_max() -> None:
    """``ultra`` is not in pi's ladder; it clamps to ``max`` (the top rung)."""
    assert to_pi_thinking_level("ultra") == "max"


def test_nearest_pi_thinking_level_clamps_to_reported_levels() -> None:
    """An unsupported rung clamps to the closest one pi reports, ties downward."""
    assert nearest_pi_thinking_level("high", ["off", "low", "high"]) == "high"
    assert nearest_pi_thinking_level("xhigh", ["off", "low", "high"]) == "high"
    assert nearest_pi_thinking_level("max", ["off", "minimal"]) == "minimal"
    # medium is equidistant from low and high → the lower rung wins.
    assert nearest_pi_thinking_level("medium", ["low", "high"]) == "low"
    # Nothing reported (or an unknown rung) → the caller leaves the level alone.
    assert nearest_pi_thinking_level("high", []) is None
    assert nearest_pi_thinking_level("bogus", ["low"]) is None


def test_efforts_for_harness_distinguishes_unsupported_from_unknown() -> None:
    """The three outcomes are distinct, and the last two must not be conflated.

    A harness with no effort plumbing returns an empty set (callers should
    refuse or filter), while a harness absent from the capability registry
    returns ``None`` (callers cannot classify it, so a filter must pass the
    value through rather than drop what a plugin harness might accept).
    """
    from omnigent.reasoning_effort import GEMINI_EFFORTS, efforts_for_harness

    assert efforts_for_harness("claude-sdk") == ANTHROPIC_EFFORTS
    assert efforts_for_harness("claude-native") == ANTHROPIC_EFFORTS
    assert efforts_for_harness("antigravity-native") == GEMINI_EFFORTS
    # pi declares its own family, so it resolves to a real vocabulary.
    assert efforts_for_harness("pi") == PI_EFFORTS
    assert efforts_for_harness("pi-native") == PI_EFFORTS
    # Known, but declared EffortFamily.NONE.
    assert efforts_for_harness("opencode-native") == frozenset()
    # Not in the registry at all — unclassifiable, not unsupported.
    assert efforts_for_harness("some-plugin-harness") is None
    assert efforts_for_harness(None) is None


def test_efforts_for_harness_resolves_aliases() -> None:
    """An alias resolves to the same vocabulary as its canonical name."""
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.reasoning_effort import efforts_for_harness

    canonical = canonicalize_harness("claude-code") or "claude-code"
    assert efforts_for_harness("claude-code") == efforts_for_harness(canonical)
