from __future__ import annotations

import pytest

from omnigent.claude_model_vocabulary import _CATALOG_PREFIXES as _CLAUDE_PREFIXES
from omnigent.codex_model_vocabulary import (
    _CATALOG_PREFIXES,
    EXTENDED_CATALOG_MODELS,
    EXTENDED_MODEL_DEFAULT_EFFORT,
    EXTENDED_MODEL_EFFORTS,
    clamp_spawn_effort,
    codex_reachable_model_slug,
    codex_spawn_model,
    comparable_model_id,
)
from omnigent.reasoning_effort import clamp_effort_for_model

# A live ``model/list`` response from a databricks-gateway codex session:
# codex spells versions with dots, and the extended-catalog row keeps its
# catalog spelling because that IS codex's id for it.
_CATALOG: list[dict[str, object]] = [
    {"id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "isDefault": True},
    {"id": "gpt-5.6-luna", "model": "gpt-5.6-luna"},
    {"id": "system.ai.glm-5-2", "model": "system.ai.glm-5-2"},
    {"id": "gpt-5.5", "model": "gpt-5.5"},
]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Codex dots the version segment and keeps the tier hyphenated.
        ("databricks-gpt-5-6-luna", "gpt-5.6-luna"),
        ("databricks-gpt-5-6-sol", "gpt-5.6-sol"),
        ("databricks-gpt-5-6-terra", "gpt-5.6-terra"),
        ("databricks-gpt-5-5", "gpt-5.5"),
        ("databricks-gpt-5-2", "gpt-5.2"),
        # Already a slug, so translating is a no-op.
        ("gpt-5.6-luna", "gpt-5.6-luna"),
        # GLM is spawnable only under the id the gateway serves it as, which
        # is the slug omnigent writes into the session's catalog.
        ("databricks-glm-5-2", "system.ai.glm-5-2"),
        ("system.ai.glm-5-2", "system.ai.glm-5-2"),
        ("glm-5-2", "system.ai.glm-5-2"),
        # No slug at all: the caller falls open rather than sending a value
        # codex rejects client-side.
        ("databricks-claude-sonnet-5", None),
        ("databricks-kimi-k2-6", None),
        ("", None),
    ],
)
def test_codex_spawn_model_speaks_the_spawn_tools_vocabulary(
    model: str,
    expected: str | None,
) -> None:
    assert codex_spawn_model(model) == expected


@pytest.mark.parametrize(
    ("effort", "model", "expected"),
    [
        # Codex refuses xhigh/max for glm, so a session default coerces down.
        ("xhigh", "system.ai.glm-5-2", "medium"),
        ("max", "system.ai.glm-5-2", "medium"),
        # Inside the ladder: the caller's pick is kept.
        ("high", "system.ai.glm-5-2", "high"),
        ("low", "system.ai.glm-5-2", "low"),
        # A model with no declared ladder is not second-guessed.
        ("xhigh", "gpt-5.6-luna", "xhigh"),
        # Nothing to clamp.
        (None, "system.ai.glm-5-2", None),
        ("xhigh", None, "xhigh"),
    ],
)
def test_clamp_spawn_effort_keeps_the_pairing_servable(
    effort: str | None,
    model: str | None,
    expected: str | None,
) -> None:
    assert clamp_spawn_effort(effort, model) == expected


def test_codex_effort_clamp_matches_the_runtime_clamp() -> None:
    # The hook's stdlib-only copy must agree with the runtime's clamp, or a
    # spawn and a turn on the same model would disagree about the effort.
    for model in EXTENDED_CATALOG_MODELS.values():
        for effort in ("low", "medium", "high", "xhigh", "max"):
            assert clamp_spawn_effort(effort, model) == clamp_effort_for_model(effort, model)


def test_every_extended_model_declares_a_ladder_and_a_fallback() -> None:
    # A model added to codex's catalog without both is one codex will refuse
    # at some effort with nothing to coerce to.
    for bare in EXTENDED_CATALOG_MODELS:
        assert EXTENDED_MODEL_EFFORTS.get(bare)
        fallback = EXTENDED_MODEL_DEFAULT_EFFORT.get(bare)
        assert fallback in EXTENDED_MODEL_EFFORTS[bare]


def test_comparable_model_id_folds_prefix_dots_and_case() -> None:
    assert comparable_model_id("databricks-gpt-5-6-luna") == "gpt-5-6-luna"
    assert comparable_model_id("gpt-5.6-luna") == "gpt-5-6-luna"
    assert comparable_model_id("system.ai.glm-5-2") == "glm-5-2"
    assert comparable_model_id("Databricks-GPT-5-6-Sol[1M]") == "gpt-5-6-sol"


def test_catalog_id_translates_to_the_codex_slug() -> None:
    assert codex_reachable_model_slug("databricks-gpt-5-6-luna", _CATALOG) == "gpt-5.6-luna"
    assert codex_reachable_model_slug("databricks-gpt-5-6-sol", _CATALOG) == "gpt-5.6-sol"
    assert codex_reachable_model_slug("databricks-gpt-5-5", _CATALOG) == "gpt-5.5"


def test_a_codex_slug_translates_to_itself() -> None:
    assert codex_reachable_model_slug("gpt-5.6-luna", _CATALOG) == "gpt-5.6-luna"


def test_an_extended_catalog_id_is_already_the_slug() -> None:
    """glm is listed under its catalog spelling, so it must survive intact."""
    assert codex_reachable_model_slug("system.ai.glm-5-2", _CATALOG) == "system.ai.glm-5-2"
    assert codex_reachable_model_slug("glm-5-2", _CATALOG) == "system.ai.glm-5-2"


def test_an_id_no_row_names_is_not_reachable() -> None:
    """A pick outside the live catalog is a decline, not a switch to attempt."""
    assert codex_reachable_model_slug("databricks-claude-opus-5", _CATALOG) is None
    # An empty catalog names nothing, so nothing is reachable through it.
    assert codex_reachable_model_slug("databricks-gpt-5-6-luna", []) is None
    assert codex_reachable_model_slug("", _CATALOG) is None


def test_a_row_naming_a_separate_servable_id_matches_on_either_side() -> None:
    options = [{"id": "gpt-5.5", "model": "databricks-gpt-5-5"}]

    assert codex_reachable_model_slug("databricks-gpt-5-5", options) == "gpt-5.5"
    assert codex_reachable_model_slug("gpt-5.5", options) == "gpt-5.5"


def test_malformed_rows_are_skipped() -> None:
    options: list[object] = ["nonsense", {"model": "gpt-5.6-luna"}, {"id": "  "}]

    assert codex_reachable_model_slug("databricks-gpt-5-6-luna", options) is None  # type: ignore[arg-type]


def test_catalog_prefixes_match_the_claude_vocabulary() -> None:
    # Both hook-side vocabularies strip the same prefixes; a drift would make
    # one harness resolve an id the other cannot.
    assert _CATALOG_PREFIXES == _CLAUDE_PREFIXES


def test_catalog_prefixes_match_the_routing_defaults() -> None:
    """This module duplicates the prefix list to stay stdlib-only; keep it equal."""
    from omnigent.server.smart_routing import MODEL_ID_PREFIXES

    assert _CATALOG_PREFIXES == MODEL_ID_PREFIXES
