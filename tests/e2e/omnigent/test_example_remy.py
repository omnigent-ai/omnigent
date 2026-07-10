"""Structural test for the Remy long-term memory example (examples/remy).

Remy is a single-agent assistant backed by the Hindsight memory builtins
(hindsight_recall / hindsight_retain / hindsight_reflect). Pure spec-load
-- no LLM, no credentials, no Hindsight API key required.

What breaks if this fails:
- a memory builtin is dropped or renamed (Remy loses recall, retain, or reflect),
- the shared bank_id changes (Remy's memory bank splits across runs),
- the harness changes away from claude-sdk (Remy loses its intended provider),
- the agent is silently pinned to a specific model (re-coupling it to one
  provider breaks users on other Claude backends).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_remy.py -> repo root is 3 parents up.
_REMY_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "remy"


@pytest.fixture(scope="module")
def remy_spec() -> AgentSpec:
    """Load and validate the remy bundle once for the module.

    expand_env=False so the structural tests run without a live HINDSIGHT_API_KEY.
    """
    return load(_REMY_BUNDLE, expand_env=False)


def test_remy_name_and_harness(remy_spec: AgentSpec) -> None:
    """Remy runs on the claude-sdk harness with no model pinned."""
    assert remy_spec.name == "remy"
    assert remy_spec.executor.config.get("harness") == "claude-sdk"
    assert remy_spec.executor.model is None


def test_remy_memory_builtins_present(remy_spec: AgentSpec) -> None:
    """All three Hindsight memory builtins are registered."""
    names = [b.name for b in remy_spec.tools.builtins]
    assert "hindsight_recall" in names
    assert "hindsight_retain" in names
    assert "hindsight_reflect" in names


def test_remy_memory_builtins_share_bank(remy_spec: AgentSpec) -> None:
    """All three builtins use the same bank_id so memory is shared across runs."""
    memory_tools = [
        b
        for b in remy_spec.tools.builtins
        if b.name in {"hindsight_recall", "hindsight_retain", "hindsight_reflect"}
    ]
    assert len(memory_tools) == 3, "Expected exactly 3 Hindsight builtins"
    bank_ids = {b.config.get("bank_id") for b in memory_tools}
    assert bank_ids == {"remy"}, f"All memory builtins must share bank_id 'remy'; got {bank_ids}"


def test_remy_has_no_sub_agents(remy_spec: AgentSpec) -> None:
    """Remy is a single agent -- no sub-agents."""
    assert remy_spec.sub_agents == []
