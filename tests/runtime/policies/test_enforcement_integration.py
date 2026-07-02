"""
Integration tests for the full policy pipeline (Phase 5).

Loads agent fixtures ported from omnigent examples, builds
PolicyEngine via the real ``build_policy_engine``, and
exercises every declared policy through the ``_enforce_policy``
entry point that the workflow will use in later phases.

These tests DO touch the real persistence layer (SQLAlchemy
store), DO run the parser + validator, and DO exercise the
engine's composition semantics against real spec instances.
They are the closest thing to full e2e coverage available
before the workflow wiring lands — if a production agent
declared any of the three fixture YAMLs, it would behave
exactly as asserted here.

Fixture parity with omnigent example YAMLs:

- ``tests/_fixtures/agents/policies-demo/`` ↔
  ``omnigent/examples/agent_with_policies.yaml``
- ``tests/_fixtures/agents/rate-limited-search/`` ↔
  ``omnigent/examples/rate_limited_search_agent.yaml``
- ``tests/_fixtures/agents/secure-research/`` ↔
  ``omnigent/examples/secure_research_agent.yaml``

Corresponding omnigent test cases ported:
- ``test_label_examples.py::test_first_db_query_allowed_but_escalates``
- ``test_label_examples.py::test_second_db_query_requires_ask``
- ``test_label_examples.py::test_full_pipeline_happy_path``
- ``test_label_examples.py::test_direct_pii_blocks_external``
- ``test_label_examples.py::test_volume_limit_triggers_after_10``
- ``test_label_examples.py::test_clean_agent_calls_freely``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import (
    _enforce_policy,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec import load
from omnigent.spec.types import (
    Phase,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# Fixtures directory — same parent as this file's repo root.
_FIXTURES = Path(__file__).resolve().parents[2] / "_fixtures" / "agents"


def _load_engine(
    fixture: str,
    store: SqlAlchemyConversationStore,
) -> PolicyEngine:
    """
    Parse an agent fixture and build a real PolicyEngine.

    Uses the same code path a production workflow would:
    parse → build_policy_engine → engine ready to evaluate.

    :param fixture: Subdirectory name under
        ``tests/_fixtures/agents/``.
    :param store: Conversation store for the engine to write
        labels through.
    :returns: PolicyEngine bound to a fresh conversation.
    """
    spec = load(_FIXTURES / fixture)
    conv = store.create_conversation()
    return build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=store,
    )


def _tool_ctx(
    name: str,
    args: dict[str, object] | None = None,
) -> EvaluationContext:
    """Build a TOOL_CALL evaluation context mirroring what the workflow assembles."""
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": name, "arguments": args or {}},
        tool_name=name,
    )


# ──────────────────────────────────────────────────────────
# policies-demo fixture (agent_with_policies.yaml parity)
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policies_demo_allows_short_sleep(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Sleep with a short duration passes through the
    FunctionPolicy. Mirrors the omnigent "Allowed" usage
    example at the top of agent_with_policies.yaml."""
    engine = _load_engine("policies-demo", conversation_store)
    result = await _enforce_policy(
        engine,
        _tool_ctx("sleep", {"seconds": 2}),
    )
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_policies_demo_denies_long_sleep(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Sleep over the threshold blocks. Mirrors the
    omnigent "Blocked tool call" example at the top of
    agent_with_policies.yaml."""
    engine = _load_engine("policies-demo", conversation_store)
    result = await _enforce_policy(
        engine,
        _tool_ctx("sleep", {"seconds": 8}),
    )
    assert result.action == PolicyAction.DENY
    # Reason mentions the offending duration — operators
    # debugging a blocked tool can see what drove the block.
    assert "8" in result.reason
    # Deciding policy is the FunctionPolicy block_long_sleep.
    assert result.deciding_policy == "block_long_sleep"


@pytest.mark.asyncio
async def test_policies_demo_taint_then_ask_shell(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Composition: web_search taints integrity to "0";
    subsequent run_shell matches `confirm_shell_after_taint`'s
    condition → ASK. Demonstrates cross-phase label propagation
    driving a condition gate."""
    engine = _load_engine("policies-demo", conversation_store)

    # Turn 1: web_search taints integrity.
    r1 = await _enforce_policy(engine, _tool_ctx("web_search", {"q": "x"}))
    assert r1.action == PolicyAction.ALLOW
    assert engine.labels["integrity"] == "0"

    # Turn 2: run_shell → ASK because condition matches now.
    r2 = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"cmd": "ls"}),
    )
    assert r2.action == PolicyAction.ASK
    assert r2.deciding_policy == "confirm_shell_after_taint"
    # ASK does NOT apply any accumulated writes — critical
    # property tested here at the e2e layer too.
    assert conversation_store.get_conversation(
        engine.conversation_id,
    ).labels == {"integrity": "0"}


@pytest.mark.asyncio
async def test_policies_demo_initial_label_seeded(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The declared initial integrity="1" is seeded on
    engine build. Without this, the condition gate on
    taint_web would never activate correctly (the label
    would be absent, not "1")."""
    engine = _load_engine("policies-demo", conversation_store)
    # Matches YAML declaration.
    assert engine.labels == {"integrity": "1"}


# ──────────────────────────────────────────────────────────
# rate-limited-search fixture
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limited_search_first_three_allowed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent ``test_first_db_query_allowed_but_escalates``
    semantics. The first N calls pass; calls beyond the budget
    ASK for approval (not DENY — lets the user extend the run
    interactively)."""
    engine = _load_engine("rate-limited-search", conversation_store)

    # 3 allowed calls.
    for i in range(3):
        r = await _enforce_policy(
            engine,
            _tool_ctx("web_search", {"q": f"query {i}"}),
        )
        # Explicit per-iteration message so the failing index
        # is obvious on regression.
        assert r.action == PolicyAction.ALLOW, (
            f"Call #{i + 1} should have been allowed; got {r.action}"
        )


@pytest.mark.asyncio
async def test_rate_limited_search_fourth_asks(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent ``test_second_db_query_requires_ask``
    for our budget=3 policy: the 4th call crosses the budget
    and the FunctionPolicy returns ASK."""
    engine = _load_engine("rate-limited-search", conversation_store)

    # Exhaust budget (calls 1-3 ALLOW).
    for _ in range(3):
        await _enforce_policy(
            engine,
            _tool_ctx("web_search", {"q": "x"}),
        )
    # 4th call asks.
    r = await _enforce_policy(
        engine,
        _tool_ctx("web_search", {"q": "once more"}),
    )
    assert r.action == PolicyAction.ASK
    # Reason mentions the exhaustion number for operator clarity.
    assert "4" in r.reason
    assert r.deciding_policy == "search_rate_limit"


@pytest.mark.asyncio
async def test_rate_limited_search_other_tools_not_gated(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Only web_search is rate-limited; other tools pass
    freely regardless. The selector's tool-name narrowing
    is load-bearing."""
    engine = _load_engine("rate-limited-search", conversation_store)

    # Exhaust web_search budget.
    for _ in range(4):
        await _enforce_policy(
            engine,
            _tool_ctx("web_search", {"q": "x"}),
        )
    # A different tool passes — not gated by this policy.
    r = await _enforce_policy(
        engine,
        _tool_ctx("summarize", {"text": "y"}),
    )
    assert r.action == PolicyAction.ALLOW


# ──────────────────────────────────────────────────────────
# secure-research fixture (full IFC scenario)
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secure_research_initial_labels_seeded(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Two declared labels are seeded to their initial values at build time."""
    engine = _load_engine("secure-research", conversation_store)
    assert engine.labels == {"confidentiality": "0", "integrity": "1"}


@pytest.mark.asyncio
async def test_secure_research_clean_agent_allows_shell(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent ``test_clean_agent_calls_freely``. An
    agent that has not touched web_search or confidential
    reads can run shell commands unconditionally."""
    engine = _load_engine("secure-research", conversation_store)
    r = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"cmd": "ls"}),
    )
    # All three enforcement policies skip: neither condition
    # matches (integrity="1", confidentiality="0") → ALLOW.
    assert r.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_secure_research_web_then_shell_asks(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Web search taints integrity → subsequent shell is
    ASK (low-integrity enforcement). Ports a happy-path slice
    of omnigent ``test_full_pipeline_happy_path``."""
    engine = _load_engine("secure-research", conversation_store)

    # web_search: ALLOW + integrity→0.
    r1 = await _enforce_policy(
        engine,
        _tool_ctx("web_search", {"q": "q"}),
    )
    assert r1.action == PolicyAction.ALLOW
    assert engine.labels["integrity"] == "0"

    # run_shell: ASK because low-integrity enforcement fires.
    r2 = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"cmd": "ls"}),
    )
    assert r2.action == PolicyAction.ASK
    assert r2.deciding_policy == "ask_low_integrity"


@pytest.mark.asyncio
async def test_secure_research_doc_then_shell_asks(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Confidential read taints confidentiality →
    subsequent shell is ASK (high-confidentiality
    enforcement). Mirror of the web-then-shell case on the
    other label axis."""
    engine = _load_engine("secure-research", conversation_store)

    r1 = await _enforce_policy(
        engine,
        _tool_ctx("read_internal_doc", {"id": "x"}),
    )
    assert r1.action == PolicyAction.ALLOW
    assert engine.labels["confidentiality"] == "1"

    r2 = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"cmd": "ls"}),
    )
    assert r2.action == PolicyAction.ASK
    # High-confidentiality is the first ASKing policy in
    # YAML order → it wins deciding_policy.
    assert r2.deciding_policy == "ask_high_confidentiality"


@pytest.mark.asyncio
async def test_secure_research_both_taints_deny_shell(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent
    ``test_indirect_pii_plus_external_asks_on_write`` shape
    for our labels. When BOTH integrity and confidentiality
    are tainted, the stricter DENY policy fires (first in
    YAML order) and short-circuits the ASKs."""
    engine = _load_engine("secure-research", conversation_store)

    await _enforce_policy(engine, _tool_ctx("web_search", {"q": "x"}))
    await _enforce_policy(engine, _tool_ctx("read_internal_doc", {"id": "d"}))
    # Now integrity=0 AND confidentiality=1 → DENY.
    r = await _enforce_policy(
        engine,
        _tool_ctx("run_shell", {"cmd": "ls"}),
    )
    assert r.action == PolicyAction.DENY
    assert r.deciding_policy == "deny_contaminated_shell"


@pytest.mark.asyncio
async def test_secure_research_write_file_gated_like_shell(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`write_file` is bundled with `run_shell` in the
    enforcement selectors, so it inherits the same
    tainted-state behavior. Load-bearing for the agent's
    "prevent exfiltration via file writes" promise."""
    engine = _load_engine("secure-research", conversation_store)

    # Taint integrity only.
    await _enforce_policy(engine, _tool_ctx("web_search", {"q": "x"}))
    r = await _enforce_policy(
        engine,
        _tool_ctx("write_file", {"path": "out.txt", "content": "x"}),
    )
    assert r.action == PolicyAction.ASK
    assert r.deciding_policy == "ask_low_integrity"


@pytest.mark.asyncio
async def test_secure_research_taint_persists(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Once integrity drops to "0" via web_search taint,
    the value is persisted and reflected in the store."""
    engine = _load_engine("secure-research", conversation_store)

    await _enforce_policy(engine, _tool_ctx("web_search", {"q": "x"}))
    assert engine.labels["integrity"] == "0"
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv.labels["integrity"] == "0"
