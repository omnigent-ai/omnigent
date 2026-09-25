"""Tests for omnigent.harnesses.pi_native.model_scope (Pi enabledModels mirror)."""

from __future__ import annotations

import time

from omnigent.harnesses.pi_native.model_scope import ScopableModel, scope_models

_SONNET = ScopableModel("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5")
_OPUS = ScopableModel("anthropic", "claude-opus-4-6", "Claude Opus 4.6")
_DATED_OPUS = ScopableModel("anthropic", "claude-opus-4-6-20260115", "Claude Opus 4.6 (dated)")
_GPT = ScopableModel("openai", "gpt-5.2", "GPT-5.2")
_ROUTED_JAMBA = ScopableModel("openrouter", "ai21/jamba-large-1.7", "AI21: Jamba Large 1.7")
_ROUTED_GLM = ScopableModel("openrouter", "z-ai/glm-5.3:batch", "Z.ai: GLM 5.3 (batch)")

_CATALOG = [_SONNET, _OPUS, _DATED_OPUS, _GPT, _ROUTED_JAMBA, _ROUTED_GLM]


def test_exact_reference_selects_one_model() -> None:
    """A canonical ``provider/id`` pattern selects exactly that model."""
    assert scope_models(["anthropic/claude-sonnet-4-5"], _CATALOG) == [_SONNET]


def test_exact_reference_is_case_insensitive() -> None:
    """Pi matches references case-insensitively; the mirror must too."""
    assert scope_models(["Anthropic/Claude-Sonnet-4-5"], _CATALOG) == [_SONNET]


def test_bare_id_selects_when_unambiguous() -> None:
    """A bare model id resolves when only one provider carries it."""
    assert scope_models(["gpt-5.2"], _CATALOG) == [_GPT]


def test_bare_id_ambiguous_falls_back_to_substring_pick() -> None:
    """A bare id served by several providers is no exact match; Pi then picks
    one via the substring fallback (first among tying alias ids)."""
    catalog = [_SONNET, ScopableModel("openrouter", "claude-sonnet-4-5", "Sonnet (routed)")]
    assert scope_models(["claude-sonnet-4-5"], catalog) == [_SONNET]


def test_multi_segment_reference_splits_on_first_slash() -> None:
    """``openrouter/ai21/jamba-large-1.7`` names provider + slash-bearing id."""
    assert scope_models(["openrouter/ai21/jamba-large-1.7"], _CATALOG) == [_ROUTED_JAMBA]


def test_single_star_does_not_cross_slash() -> None:
    """``openrouter/*`` must not select multi-segment openrouter ids (minimatch)."""
    assert scope_models(["openrouter/*"], _CATALOG) == []


def test_globstar_crosses_segments() -> None:
    """``openrouter/**`` selects everything under the provider."""
    assert scope_models(["openrouter/**"], _CATALOG) == [_ROUTED_JAMBA, _ROUTED_GLM]


def test_glob_matches_qualified_or_bare_id() -> None:
    """``*sonnet*`` matches without requiring the provider prefix."""
    assert scope_models(["*sonnet*"], _CATALOG) == [_SONNET]


def test_provider_glob_selects_provider_models() -> None:
    """``anthropic/*`` selects the provider's (single-segment) models."""
    assert scope_models(["anthropic/*"], _CATALOG) == [_SONNET, _OPUS, _DATED_OPUS]


def test_thinking_level_suffix_is_stripped() -> None:
    """``provider/id:high`` scopes the model; the level is Pi-side only."""
    assert scope_models(["anthropic/claude-sonnet-4-5:high"], _CATALOG) == [_SONNET]
    assert scope_models(["anthropic/*:high"], _CATALOG) == [_SONNET, _OPUS, _DATED_OPUS]


def test_substring_fallback_prefers_alias_over_dated() -> None:
    """A partial pattern picks one model, preferring the undated alias id."""
    assert scope_models(["opus"], _CATALOG) == [_OPUS]


def test_patterns_union_without_duplicates() -> None:
    """Several patterns union their selections, keeping first-match order."""
    patterns = ["anthropic/claude-sonnet-4-5", "*sonnet*", "openai/gpt-5.2"]
    assert scope_models(patterns, _CATALOG) == [_SONNET, _GPT]


def test_unmatched_patterns_select_nothing() -> None:
    """Patterns that match nothing yield an empty scope (callers fall back)."""
    assert scope_models(["mistral/devstral-large"], _CATALOG) == []


def test_invalid_character_class_selects_nothing_without_raising() -> None:
    """A class the regex engine rejects matches nothing, like minimatch.

    minimatch resolves a reversed range (``[z-a]``) to "matches nothing";
    Python's ``re`` raises on it instead. The mirror must swallow that
    difference: a bad class silently selects nothing rather than crashing
    catalog resolution for the whole picker.
    """
    assert scope_models(["[z-a]"], _CATALOG) == []
    assert scope_models(["x[b-a]y"], _CATALOG) == []
    assert scope_models(["anthropic/[z-a]*"], _CATALOG) == []


def test_invalid_class_pattern_keeps_other_patterns_selecting() -> None:
    """One bad pattern never poisons the rest of the curation."""
    assert scope_models(["[z-a]", "anthropic/claude-sonnet-4-5"], _CATALOG) == [_SONNET]


def test_wildcard_heavy_patterns_resolve_without_backtracking_blowup() -> None:
    """Adjacent/interleaved wildcards must not backtrack catastrophically.

    ``********X`` against a long id used to hang the naive translation
    (each ``*`` compiled to an independent ``[^/]*``). Wall-clock bound: the
    hardened translation resolves these in microseconds; well under a second
    even on a loaded machine, versus effectively-forever before.
    """
    long_id_catalog = [*_CATALOG, ScopableModel("openai", "a" * 80, "long id")]
    start = time.perf_counter()
    assert scope_models(["*" * 8 + "X"], long_id_catalog) == []
    assert scope_models(["*?" * 8 + "X"], long_id_catalog) == []
    assert scope_models(["*a*a*a*a*a*a*X"], long_id_catalog) == []
    assert time.perf_counter() - start < 5.0


def test_wildcard_runs_still_match_like_single_stars() -> None:
    """Collapsed ``*`` runs keep single-star semantics."""
    assert scope_models(["anthropic/claude-s***5"], _CATALOG) == [_SONNET]
    assert scope_models(["anthropic/claude-[!o]*"], _CATALOG) == [_SONNET]
