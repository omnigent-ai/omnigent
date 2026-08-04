"""Structural test for the Remy Auto automatic-memory example (examples/remy_auto).

Remy Auto is the runtime-``memory:``-block counterpart to ``remy``: same
"assistant that remembers", but recall/retain are automatic (handled by the
runner) instead of exposed as ``hindsight_*`` tools the model must call. Pure
spec-load -- no LLM, no credentials, no Hindsight API key required.

What breaks if this fails:
- the ``memory:`` block is dropped or disabled (Remy Auto stops remembering),
- ``auto_recall`` / ``auto_retain`` are silently flipped off,
- the shared bank_id changes (memory splits across runs),
- a ``hindsight_*`` tool leaks back in (this example is deliberately tool-free —
  its whole point is that memory needs no tools),
- the harness changes away from claude-sdk, or the agent is pinned to a model
  (re-coupling it to one provider).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_remy_auto.py -> repo root is 3 parents up.
_REMY_AUTO_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "remy_auto"


@pytest.fixture(scope="module")
def remy_auto_spec() -> AgentSpec:
    """Load and validate the remy_auto bundle once for the module.

    expand_env=False so the structural tests run without a live HINDSIGHT_API_KEY;
    the ``${HINDSIGHT_API_KEY}`` reference is preserved verbatim.
    """
    return load(_REMY_AUTO_BUNDLE, expand_env=False)


def test_remy_auto_name_and_harness(remy_auto_spec: AgentSpec) -> None:
    """Remy Auto runs on the claude-sdk harness with no model pinned."""
    assert remy_auto_spec.name == "remy-auto"
    assert remy_auto_spec.executor.config.get("harness") == "claude-sdk"
    assert remy_auto_spec.executor.model is None


def test_remy_auto_memory_block_enabled(remy_auto_spec: AgentSpec) -> None:
    """The automatic memory layer is configured and both phases are on."""
    memory = remy_auto_spec.memory
    assert memory is not None, "Remy Auto must declare a memory: block"
    assert memory.enabled is True
    assert memory.auto_recall is True
    assert memory.auto_retain is True
    assert memory.bank_id == "remy-auto"
    assert memory.provider == "hindsight"


def test_remy_auto_api_key_uses_env_reference(remy_auto_spec: AgentSpec) -> None:
    """The example must not hardcode a key -- it references the env var."""
    assert remy_auto_spec.memory is not None
    assert remy_auto_spec.memory.api_key == "${HINDSIGHT_API_KEY}"


def test_remy_auto_has_no_memory_tools(remy_auto_spec: AgentSpec) -> None:
    """The whole point: automatic memory needs no hindsight_* tools declared."""
    names = {b.name for b in remy_auto_spec.tools.builtins}
    assert not (names & {"hindsight_recall", "hindsight_retain", "hindsight_reflect"}), (
        f"remy_auto must declare no Hindsight tools; got {names}"
    )


def test_remy_auto_has_no_sub_agents(remy_auto_spec: AgentSpec) -> None:
    """Remy Auto is a single agent -- no sub-agents."""
    assert remy_auto_spec.sub_agents == []
