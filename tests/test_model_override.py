"""Unit tests for ``omnigent/model_override.py``.

The validator guards a spawn boundary: a model override is persisted on
the session row and later becomes a ``--model`` argv element (native
CLIs) or a ``HARNESS_<H>_MODEL`` env var (SDK harnesses). These tests
pin the accepted charset, the loud rejections, and the harness-support
map that ``sys_session_send`` consults before forwarding an override.
"""

from __future__ import annotations

import pytest

from omnigent.model_override import (
    MODEL_OVERRIDE_MAX_LEN,
    canonical_model_spelling,
    harness_supports_model_override,
    model_family_mismatch,
    normalize_model_for_provider,
    validate_model_override,
)


@pytest.mark.parametrize(
    "value",
    [
        "databricks-claude-sonnet-4-6",
        "claude-opus-4-8[1m]",
        "gpt-5.4-mini",
        "openai/gpt-4o",
        "us.anthropic.claude-sonnet-4-6",
        "databricks/databricks-gpt-5-4",
        "vendor:tag",
        "o3",
    ],
)
def test_validate_model_override_accepts_real_id_shapes(value: str) -> None:
    """
    Every real-world model-id shape passes unchanged.

    A failure means the charset regressed and a legitimate dispatch
    (e.g. a bracketed context-window suffix) would be rejected.
    """
    assert validate_model_override(value) == value


def test_validate_model_override_strips_whitespace() -> None:
    """Surrounding whitespace is stripped, mirroring the PATCH path."""
    assert validate_model_override("  claude-opus-4-8  ") == "claude-opus-4-8"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        # Shell metacharacters must never reach a command line.
        "claude; rm -rf /",
        "claude && evil",
        "model`id`",
        'model"quoted"',
        "model with spaces",
        "model\nnewline",
        # Flag-shaped values could be parsed as a CLI option.
        "--dangerously-skip-permissions",
        "-flag",
        # Length cap.
        "a" * (MODEL_OVERRIDE_MAX_LEN + 1),
    ],
)
def test_validate_model_override_rejects_unsafe_values(value: str) -> None:
    """
    Empty, shell-shaped, flag-shaped, and oversized values fail loud.

    A pass-through here would let a model string smuggle argv/shell
    content across the harness spawn boundary.
    """
    with pytest.raises(ValueError):
        validate_model_override(value)


@pytest.mark.parametrize(
    "harness",
    [
        "claude-native",
        "codex-native",
        "claude-sdk",
        # The user-facing alias for claude-sdk must resolve too.
        "claude",
        "codex",
        "pi",
        "openai-agents",
        "cursor",
        "antigravity",
    ],
)
def test_harness_supports_model_override_for_plumbed_harnesses(harness: str) -> None:
    """
    Harnesses with --model / spawn-env plumbing report support.

    A False here would make ``sys_session_send`` reject a model for a
    harness that actually honors it.
    """
    assert harness_supports_model_override(harness) is True


@pytest.mark.parametrize(
    "harness",
    [
        # No model-override plumbing on the runner path: the persisted
        # value would be silently ignored.
        "databricks_supervisor",
        "totally-unknown",
        None,
    ],
)
def test_harness_supports_model_override_false_for_unplumbed(harness: str | None) -> None:
    """
    Unplumbed / unknown harnesses report no support.

    A True here would silently drop the orchestrator's model choice —
    exactly the failure mode the dispatch-time gate exists to prevent.
    """
    assert harness_supports_model_override(harness) is False


class TestModelFamilyMismatch:
    """model_family_mismatch enforces vendor families per harness."""

    @pytest.mark.parametrize(
        ("harness", "model"),
        [
            ("claude-native", "databricks-claude-sonnet-4-6"),
            ("claude-sdk", "claude-opus-4-8"),
            ("codex-native", "databricks-gpt-5-4"),
            ("codex", "gpt-5.1-codex"),
            ("openai-agents", "gpt-5.4-mini"),
            # openai-agents is multi-model like pi (a live SDK probe completed a
            # Claude tool-calling turn over the chat wire), so it accepts the
            # Claude / Kimi / Llama families the gateway also serves it.
            ("openai-agents", "databricks-claude-sonnet-4-6"),
            ("openai-agents", "databricks-kimi-k2-6"),
            ("openai-agents", "databricks-meta-llama-3.3-70b-instruct"),
            # The "-sdk" / executor-type spellings canonicalize_harness
            # passes through must be multi-model too — an earlier change had
            # added them to the GPT-only set; a later change removes every
            # openai-agents spelling so none of them family-reject a non-GPT id.
            ("openai-agents-sdk", "databricks-claude-sonnet-4-6"),
            ("openai-agents-sdk", "gpt-5.4-mini"),
            ("agents_sdk", "databricks-meta-llama-3.3-70b-instruct"),
            ("agents_sdk", "databricks-claude-opus-4-8"),
            ("pi", "databricks-claude-opus-4-8"),
            ("pi", "databricks-gpt-5-4-mini"),
            ("pi", "databricks-meta-llama-3.3-70b-instruct"),
            # antigravity is Gemini-native: expected Gemini shapes pass, and
            # so do bare/ambiguous ids the SDK legitimately accepts (only the
            # Claude/GPT/databricks-gateway families are rejected below).
            ("antigravity", "gemini-3.5-flash"),
            ("antigravity", "gemini-2.5-flash"),
            ("agy", "gemini-3.5-flash"),
            ("google-antigravity", "gemini-2.5-flash"),
            ("antigravity", "gemini-2.5-pro"),
        ],
    )
    def test_compatible_pairs_pass(self, harness: str, model: str) -> None:
        """A matching family (or a multi-model harness: pi, openai-agents) returns None."""
        assert model_family_mismatch(harness, model) is None

    @pytest.mark.parametrize(
        ("harness", "model", "expected_rule"),
        [
            ("claude-native", "databricks-gpt-5-4", "only runs Claude models"),
            ("native-claude", "gpt-5.4", "only runs Claude models"),
            ("claude-sdk", "databricks-meta-llama-3.3-70b-instruct", "only runs Claude models"),
            ("codex-native", "databricks-claude-sonnet-4-6", "only runs GPT models"),
            ("native-codex", "claude-opus-4-8", "only runs GPT models"),
            ("codex", "databricks-meta-llama-3.3-70b-instruct", "only runs GPT models"),
            # antigravity is Gemini-native: syntactically valid non-Gemini ids
            # must fail loud at the dispatch gate rather than be persisted as
            # model_override and land in HARNESS_ANTIGRAVITY_MODEL only to fail
            # later in the Gemini-native SDK path. The databricks-claude case
            # also covers the no-gateway-path prefix rejection.
            ("antigravity", "gpt-5.4-mini", "Gemini-native"),
            ("antigravity", "databricks-claude-sonnet-4-6", "Gemini-native"),
            ("antigravity", "claude-opus-4-8", "Gemini-native"),
            ("agy", "gpt-5.4-mini", "Gemini-native"),
            ("google-antigravity", "databricks-gpt-5-4", "Gemini-native"),
        ],
    )
    def test_wrong_or_unknown_family_is_rejected(
        self, harness: str, model: str, expected_rule: str
    ) -> None:
        """Cross-family and undeterminable ids are rejected with the rule named.

        The alias case (``native-claude``) proves canonicalization is
        applied before the family lookup; the llama cases prove an
        undeterminable family fails loud on single-vendor harnesses
        rather than passing through to a gateway error after spawn. The
        ``agy`` / ``google-antigravity`` cases prove the antigravity rule
        applies after harness canonicalization too.
        """
        msg = model_family_mismatch(harness, model)
        assert msg is not None
        assert expected_rule in msg
        assert model in msg


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Bare canonical vendor ids gain the gateway prefix mechanically.
        ("claude-opus-4-8", "databricks-claude-opus-4-8"),
        ("claude-sonnet-4-6", "databricks-claude-sonnet-4-6"),
        ("gpt-5-4", "databricks-gpt-5-4"),
        ("gpt-5.4-mini", "databricks-gpt-5.4-mini"),
        # Already gateway-local: no double prefix.
        ("databricks-claude-opus-4-8", "databricks-claude-opus-4-8"),
        # Non-mechanical shapes pass through to the fail-loud path:
        # vendor-prefixed, slash-routed, alias-bracketed, other-family.
        ("us.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"),
        ("openai/gpt-4o", "openai/gpt-4o"),
        ("claude-opus-4-8[1m]", "claude-opus-4-8[1m]"),
        ("kimi-k2.6", "kimi-k2.6"),
    ],
)
def test_normalize_localizes_canonical_ids_for_gateway_children(model: str, expected: str) -> None:
    """
    A Databricks-gateway child localizes bare canonical claude/gpt ids.

    A missed prefix means the child spawns on an id the gateway cannot
    route; a wrongly-added prefix on a non-mechanical shape would
    fabricate an endpoint name that does not exist.
    """
    assert normalize_model_for_provider(model, "databricks") == expected


@pytest.mark.parametrize("provider_kind", ["key", "subscription"])
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Gateway-local ids lose the prefix for vendor-direct children.
        ("databricks-claude-opus-4-8", "claude-opus-4-8"),
        ("databricks-gpt-5-4", "gpt-5-4"),
        # The stripped remainder must itself be a mechanical claude/gpt
        # id — other families have no canonical vendor counterpart.
        ("databricks-meta-llama-3.3-70b-instruct", "databricks-meta-llama-3.3-70b-instruct"),
        # Already canonical: unchanged.
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ],
)
def test_normalize_strips_gateway_prefix_for_vendor_direct_children(
    provider_kind: str, model: str, expected: str
) -> None:
    """
    Vendor-direct children (API key / CLI login) drop the gateway prefix.

    A kept prefix means the vendor API rejects the id; stripping a
    non-claude/gpt remainder would fabricate a vendor id that does not
    exist.

    :param provider_kind: The vendor-direct provider kind under test.
    :param model: The requested model id.
    :param expected: The id that must be persisted.
    """
    assert normalize_model_for_provider(model, provider_kind) == expected


@pytest.mark.parametrize("provider_kind", ["gateway", "local", "none", None])
@pytest.mark.parametrize("model", ["claude-opus-4-8", "databricks-gpt-5-4", "openai/gpt-4o"])
def test_normalize_passes_through_for_unmapped_provider_kinds(
    provider_kind: str | None, model: str
) -> None:
    """
    Non-vendor-direct gateways and undeterminable providers never transform.

    OpenRouter/LiteLLM-style endpoints use their own id namespaces
    (``anthropic/claude-...``), so no transform is mechanical there; an
    undeterminable provider must keep the fail-loud path intact.

    :param provider_kind: The provider kind under test.
    :param model: The requested model id.
    """
    assert normalize_model_for_provider(model, provider_kind) == model


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Mechanical gateway counterparts strip to the canonical id.
        ("databricks-claude-haiku-4-5", "claude-haiku-4-5"),
        ("databricks-gpt-5-4-mini", "gpt-5-4-mini"),
        # Already canonical: unchanged.
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        # Non-claude/gpt remainders have no mechanical counterpart —
        # stripping would fabricate a vendor id that does not exist.
        ("databricks-meta-llama-3.3-70b-instruct", "databricks-meta-llama-3.3-70b-instruct"),
        # Bracket/slash/dotted shapes are not mechanical either.
        ("databricks-claude-opus-4-8[1m]", "databricks-claude-opus-4-8[1m]"),
        ("openai/gpt-4o", "openai/gpt-4o"),
        ("us.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"),
    ],
)
def test_canonical_model_spelling(model: str, expected: str) -> None:
    """
    The canonicalizer strips exactly the mechanical ``databricks-``
    prefix and nothing else.

    This is the single spelling-equivalence rule shared by dispatch
    normalization and cost-tier ranking; an over-eager strip here would
    conflate distinct ids, a missed strip would re-open the cost_plan
    false-deny on mixed-fleet tier catalogs.

    :param model: The id to canonicalize.
    :param expected: The canonical spelling.
    """
    assert canonical_model_spelling(model) == expected


@pytest.mark.parametrize(
    ("harness", "model"),
    [
        # The family guard sees the PRE-normalization id; its verdict is
        # identical post-transform because the family token survives the
        # prefix in both directions.
        ("claude-native", "claude-opus-4-8"),
        ("codex-native", "gpt-5-4"),
    ],
)
def test_family_tokens_survive_normalization_in_both_directions(harness: str, model: str) -> None:
    """
    Guard-then-normalize ordering cannot change the family verdict.

    The dispatch gate documents family-guard-first (so errors quote the
    caller's exact id); this pins the property that makes the order
    safe: a compatible id stays compatible after localization, and the
    localized id round-trips back to a compatible id when stripped.
    """
    localized = normalize_model_for_provider(model, "databricks")
    assert localized == f"databricks-{model}"
    # Compatible before AND after localization.
    assert model_family_mismatch(harness, model) is None
    assert model_family_mismatch(harness, localized) is None
    # The strip direction round-trips to the original canonical id.
    assert normalize_model_for_provider(localized, "key") == model
