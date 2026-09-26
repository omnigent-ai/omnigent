"""Tests for omnigent.models.pi_model_compatibility (Pi ``models.json`` entries)."""

from __future__ import annotations

import pytest

from omnigent.models.model_catalog import ModelEntry
from omnigent.models.model_metadata import ModelCapability, ModelMetadata, ModelWireAPI
from omnigent.models.pi_model_compatibility import (
    enrich_databricks_model_catalog,
    pi_model_json_entry,
)

_REASONING = frozenset({ModelCapability.REASONING})


def _entry(model_id: str, *, reasoning: bool | None, wire: ModelWireAPI) -> ModelEntry:
    """A catalog entry whose reasoning capability is known-true, known-false, or unknown."""
    return ModelEntry(
        id=model_id,
        family="other",
        metadata=ModelMetadata(
            supported_capabilities=_REASONING if reasoning else frozenset(),
            unsupported_capabilities=_REASONING if reasoning is False else frozenset(),
            wire_apis=frozenset({wire}),
        ),
    )


@pytest.mark.parametrize(
    ("model_id", "wire"),
    [
        ("system.ai.gpt-6-luna", ModelWireAPI.OPENAI_RESPONSES),
        ("system.ai.gemini-3-8-flash", ModelWireAPI.OPENAI_CHAT),
        ("system.ai.glm-5-3", ModelWireAPI.OPENAI_RESPONSES),
        ("system.ai.qwen3-next", ModelWireAPI.OPENAI_RESPONSES),
    ],
)
def test_catalog_reasoning_capability_sets_pi_reasoning_flag(
    model_id: str, wire: ModelWireAPI
) -> None:
    """A reasoning-capable catalog entry carries ``reasoning: true`` whatever its vendor."""
    entry = pi_model_json_entry(_entry(model_id, reasoning=True, wire=wire))

    assert entry.get("reasoning") is True


def test_unknown_capability_keeps_vendor_fallbacks() -> None:
    """Without catalog capability data, only DeepSeek and Claude get the flag."""
    deepseek = pi_model_json_entry(
        _entry("system.ai.deepseek-v4", reasoning=None, wire=ModelWireAPI.OPENAI_CHAT)
    )
    claude = pi_model_json_entry(
        _entry("system.ai.claude-fable-5-1", reasoning=None, wire=ModelWireAPI.ANTHROPIC_MESSAGES)
    )
    llama = pi_model_json_entry(
        _entry("system.ai.llama-4-maverick", reasoning=None, wire=ModelWireAPI.OPENAI_CHAT)
    )

    assert deepseek.get("reasoning") is True
    assert claude.get("reasoning") is True
    assert "reasoning" not in llama


def test_catalog_unsupported_reasoning_leaves_flag_unset() -> None:
    """A model the catalog marks non-reasoning is written without the flag."""
    entry = pi_model_json_entry(
        _entry("system.ai.llama-4-maverick", reasoning=False, wire=ModelWireAPI.OPENAI_CHAT)
    )

    assert "reasoning" not in entry


def test_deepseek_keeps_reasoning_channel_flag_despite_catalog() -> None:
    """DeepSeek streams on ``reasoning_content``; Pi reads that channel only with the flag."""
    entry = pi_model_json_entry(
        _entry("databricks-deepseek-v4", reasoning=False, wire=ModelWireAPI.OPENAI_CHAT)
    )

    assert entry.get("reasoning") is True


def test_enriched_live_entry_carries_catalog_reasoning() -> None:
    """The wire-only live listing picks up the MLflow catalog's reasoning capability."""
    live = ModelEntry(
        id="system.ai.gpt-6-luna",
        family="openai",
        metadata=ModelMetadata(wire_apis=frozenset({ModelWireAPI.OPENAI_RESPONSES})),
    )
    catalog = ModelEntry(
        id="databricks-gpt-6-luna",
        family="openai",
        metadata=ModelMetadata(supported_capabilities=_REASONING, context_window=400_000),
    )

    (enriched,) = enrich_databricks_model_catalog((live,), (catalog,))

    assert enriched.metadata.supports(ModelCapability.REASONING) is True
    assert enriched.metadata.wire_apis == frozenset({ModelWireAPI.OPENAI_RESPONSES})
    assert pi_model_json_entry(enriched) == {
        "id": "system.ai.gpt-6-luna",
        "input": ["text", "image"],
        "contextWindow": 400_000,
        "reasoning": True,
    }
