"""
Conversation-isolation tests.

Verifies that PolicyEngine instances bound to different
conversations keep their label state separate — no
cross-conversation leakage via the store.

Load-bearing: omnigent runs multiple concurrent
conversations against the same database. A bug that
leaked label state across conversations would break every
per-user IFC guarantee.

Covers:
- Two engines on different conversations — writes on one
  invisible to the other.
- Label seed on a new conversation doesn't see another's
  state.
- DENY on conversation A doesn't leak labels into B's hot
  cache.
- Concurrent-build semantics (two engines built back-to-back
  on the same conversation see identical state).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import build_policy_engine
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec import load
from omnigent.spec.types import (
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.runtime.policies.conftest import make_fixed_policy

_SECURE = Path(__file__).resolve().parents[3] / "_fixtures" / "agents" / "secure-research"


@pytest.mark.asyncio
async def test_different_conversations_have_isolated_labels(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Two engines on different conversations don't share
    label state. Absolute baseline for multi-tenant
    safety."""
    spec = load(_SECURE)
    conv_a = conversation_store.create_conversation()
    conv_b = conversation_store.create_conversation()

    engine_a = build_policy_engine(
        spec=spec,
        conversation_id=conv_a.id,
        conversation_store=conversation_store,
    )
    engine_b = build_policy_engine(
        spec=spec,
        conversation_id=conv_b.id,
        conversation_store=conversation_store,
    )

    # Taint on A.
    await engine_a.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "web_search", "arguments": {"q": "x"}},
            tool_name="web_search",
        ),
    )
    assert engine_a.labels["integrity"] == "0"
    # B is untouched.
    assert engine_b.labels["integrity"] == "1"
    # Store-side round-trip confirms.
    assert conversation_store.get_conversation(conv_a.id).labels["integrity"] == "0"
    assert conversation_store.get_conversation(conv_b.id).labels["integrity"] == "1"


@pytest.mark.asyncio
async def test_deny_on_one_conversation_does_not_leak_to_other(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A DENY on conversation A shouldn't somehow change
    B's reachable state — DENY short-circuits within the
    engine instance only."""
    spec = load(_SECURE)
    conv_a = conversation_store.create_conversation()
    conv_b = conversation_store.create_conversation()

    engine_a = build_policy_engine(
        spec=spec,
        conversation_id=conv_a.id,
        conversation_store=conversation_store,
    )
    engine_b = build_policy_engine(
        spec=spec,
        conversation_id=conv_b.id,
        conversation_store=conversation_store,
    )

    # Taint both dimensions on A → now DENY on shell.
    await engine_a.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "web_search", "arguments": {}},
            tool_name="web_search",
        ),
    )
    await engine_a.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "read_internal_doc", "arguments": {}},
            tool_name="read_internal_doc",
        ),
    )
    deny_result = await engine_a.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "run_shell", "arguments": {}},
            tool_name="run_shell",
        ),
    )
    assert deny_result.action == PolicyAction.DENY

    # B — still clean — can run shell freely.
    allow_result = await engine_b.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "run_shell", "arguments": {}},
            tool_name="run_shell",
        ),
    )
    assert allow_result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_seeding_isolated_across_conversations(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Seeding on conv_a does not trigger writes on conv_b.
    Each call to ``build_policy_engine`` is scoped by
    conversation_id — proved by observing B's state is
    unchanged by A's seed."""
    spec = load(_SECURE)
    conv_a = conversation_store.create_conversation()
    conv_b = conversation_store.create_conversation()

    # Seed A. This writes {integrity: "1", confidentiality: "0"}.
    build_policy_engine(
        spec=spec,
        conversation_id=conv_a.id,
        conversation_store=conversation_store,
    )

    # B hasn't been built yet — no labels exist.
    got_b_before = conversation_store.get_conversation(conv_b.id)
    assert got_b_before is not None
    assert got_b_before.labels == {}

    # Now build B. Its own seeding lands.
    build_policy_engine(
        spec=spec,
        conversation_id=conv_b.id,
        conversation_store=conversation_store,
    )
    got_b_after = conversation_store.get_conversation(conv_b.id)
    assert got_b_after is not None
    # Same declared initials as A, but independently seeded.
    assert got_b_after.labels == {"integrity": "1", "confidentiality": "0"}


@pytest.mark.asyncio
async def test_back_to_back_builds_same_conversation_see_same_state(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Two sequential builds on the same conversation
    produce engines with identical hot caches — the seed
    path is deterministic, and the persisted state is the
    shared source of truth."""
    spec = load(_SECURE)
    conv = conversation_store.create_conversation()

    engine_1 = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    engine_2 = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )

    # Both see the seeded initials.
    assert (
        engine_1.labels
        == engine_2.labels
        == {
            "integrity": "1",
            "confidentiality": "0",
        }
    )


@pytest.mark.asyncio
async def test_parallel_conversations_with_different_specs(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Two conversations running different specs don't
    conflate their label_defs. A label key that exists in
    one spec's schema doesn't impose constraints on the
    other's writes (labels are conversation-scoped; schemas
    are spec-scoped)."""
    from omnigent.spec.types import LabelDef

    # Spec A: has a schema for `integrity` with monotonic.
    policy_a = make_fixed_policy(
        name="write_integrity",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
        set_labels={"integrity": "0"},
    )
    conv_a = conversation_store.create_conversation()
    engine_a = PolicyEngine(
        policies=[policy_a],
        label_defs={
            "integrity": LabelDef(values=["0", "1"], monotonic="decreasing"),
        },
        ask_timeout=30,
        conversation_id=conv_a.id,
        initial_labels={"integrity": "1"},
        conversation_store=conversation_store,
    )

    # Spec B: no schema — schemaless writes allowed.
    policy_b = make_fixed_policy(
        name="write_anything",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
        # B writes "integrity: 5" — which would violate
        # A's enum but B has no schema.
        set_labels={"integrity": "5"},
    )
    conv_b = conversation_store.create_conversation()
    engine_b = PolicyEngine(
        policies=[policy_b],
        label_defs={},  # No schema for B.
        ask_timeout=30,
        conversation_id=conv_b.id,
        initial_labels={},
        conversation_store=conversation_store,
    )

    # Both evaluate.
    await engine_a.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
    )
    await engine_b.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
    )

    # A's write landed (decreasing 1→0 is allowed).
    assert engine_a.labels["integrity"] == "0"
    # B's write landed too — B's schemaless policy accepts "5".
    assert engine_b.labels["integrity"] == "5"
    # Stores match.
    assert (
        conversation_store.get_conversation(
            conv_a.id,
        ).labels["integrity"]
        == "0"
    )
    assert (
        conversation_store.get_conversation(
            conv_b.id,
        ).labels["integrity"]
        == "5"
    )
