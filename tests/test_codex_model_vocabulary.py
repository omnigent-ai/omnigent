from __future__ import annotations

import pytest

from omnigent.claude_model_vocabulary import _CATALOG_PREFIXES as _CLAUDE_PREFIXES
from omnigent.codex_model_vocabulary import (
    _CATALOG_PREFIXES,
    EXTENDED_CATALOG_MODELS,
    EXTENDED_MODEL_DEFAULT_EFFORT,
    EXTENDED_MODEL_EFFORTS,
    clamp_spawn_effort,
    codex_spawn_model,
)
from omnigent.reasoning_effort import clamp_effort_for_model


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


def test_catalog_prefixes_match_the_claude_vocabulary() -> None:
    # Both hook-side vocabularies strip the same prefixes; a drift would make
    # one harness resolve an id the other cannot.
    assert _CATALOG_PREFIXES == _CLAUDE_PREFIXES
