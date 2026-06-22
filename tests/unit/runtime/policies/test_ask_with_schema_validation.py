"""
ASK flow + LabelDef schema validation composition tests.

Verifies that the schema checks engine.apply_label_writes
performs (POLICIES.md §10 — values whitelist + monotonic
direction) ALSO apply to writes that get approved through
the ASK cycle. Without this, a policy could "launder" an
invalid label write by emitting it only on ASK (where the
engine defers the write to post-approval apply).

Load-bearing: the omnigent parity promise is that the
same label write rules apply regardless of which path
(direct ALLOW vs approved ASK) carries the write.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import _await_elicitation
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    LabelDef,
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.runtime.policies.conftest import make_fixed_policy


class _Harness:
    """Minimal elicitation harness."""

    def __init__(self, verdict: str) -> None:
        """Capture a pre-canned verdict for the park callback.

        :param verdict: JSON-encoded MCP-shape ``ElicitResult``
            body, e.g. ``'{"action": "accept"}'``.
        """
        self._verdict = verdict

    def register(self, elicitation_id: str, task_id: str, params_json: str) -> None:
        """No-op register seam.

        :param elicitation_id: Generated id (unused here).
        :param task_id: Parked workflow id (unused).
        :param params_json: JSON-encoded params block (unused).
        """

    def emit(self, event: dict[str, Any]) -> None:
        """No-op emit seam.

        :param event: SSE event dict (unused).
        """

    async def park(self, elicitation_id: str, timeout_s: int) -> str:
        """Return the canned verdict immediately.

        :param elicitation_id: Generated id (unused).
        :param timeout_s: Resolved timeout (unused).
        :returns: The verdict string passed at construction.
        """
        return self._verdict


async def _drive_ask(
    engine: PolicyEngine,
    ctx: EvaluationContext,
    verdict: str,
) -> bool:
    """Evaluate, assert ASK, drive elicitation with *verdict*."""
    result = await engine.evaluate(ctx)
    assert result.action == PolicyAction.ASK
    h = _Harness(verdict)
    return await _await_elicitation(
        task_id="task_1",
        root_task_id="task_1",
        result=result,
        phase=ctx.phase,
        content_preview="x",
        policy_engine=engine,
        register=h.register,
        emit=h.emit,
        park=h.park,
    )


# ── ASK + monotonic enforcement ────────────────────────


@pytest.mark.asyncio
async def test_ask_approve_respects_monotonic_drop(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A policy ASKs to write integrity="1" on a
    decreasing-monotonic label that's already "0". Even if
    the user APPROVES, the engine's apply_label_writes drops
    the violation.

    If this regresses, an ASK-path approval could undo taint
    in a way that direct ALLOW cannot — breaking IFC parity
    between the two paths."""
    policy = make_fixed_policy(
        name="try_untaint",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ASK,
        reason="untaint",
        set_labels={"integrity": "1"},  # attempting to restore
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={
            "integrity": LabelDef(
                values=["0", "1"],
                monotonic="decreasing",
            ),
        },
        ask_timeout=30,
        conversation_id=conv.id,
        # Pre-tainted — integrity already at "0".
        initial_labels={"integrity": "0"},
        conversation_store=conversation_store,
    )

    # Pre-seed the store so we can check persisted state
    # matches the hot cache.
    conversation_store.set_labels(conv.id, {"integrity": "0"})

    approved = await _drive_ask(
        engine,
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        '{"action": "accept"}',
    )
    # User approved, but the write violated monotonic → dropped.
    assert approved is True
    # Hot cache unchanged — "1" was dropped.
    assert engine.labels["integrity"] == "0"
    # Persisted state matches — the drop prevented the
    # rogue "1" from overwriting the stored "0".
    conv_refetch = conversation_store.get_conversation(conv.id)
    assert conv_refetch is not None
    assert conv_refetch.labels["integrity"] == "0"


@pytest.mark.asyncio
async def test_ask_approve_respects_enum_constraint(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Same shape for enum violations: approved ASK writes
    an out-of-enum value → engine drops it."""
    policy = make_fixed_policy(
        name="rogue_value",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ASK,
        reason="approve me",
        # Value NOT in declared enum.
        set_labels={"role": "root"},
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={"role": LabelDef(values=["admin", "user"])},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )

    approved = await _drive_ask(
        engine,
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        '{"action": "accept"}',
    )
    assert approved is True
    # Dropped — role never set.
    assert "role" not in engine.labels


@pytest.mark.asyncio
async def test_ask_approve_mixed_valid_invalid_batch(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """An approved ASK with multiple set_labels: valid keys
    land, invalid keys drop (per-key silent drop). Proves
    the ASK path delegates to the same _filter_schema_valid
    helper as direct ALLOW."""
    policy = make_fixed_policy(
        name="mixed",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ASK,
        reason="approve",
        set_labels={
            "integrity": "1",  # monotonic violation
            "role": "admin",  # valid
            "schemaless": "free",  # unknown key, set freely
        },
    )
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={
            "integrity": LabelDef(
                values=["0", "1"],
                monotonic="decreasing",
            ),
            "role": LabelDef(values=["admin", "user"]),
        },
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={"integrity": "0"},
        conversation_store=conversation_store,
    )

    approved = await _drive_ask(
        engine,
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        '{"action": "accept"}',
    )
    assert approved is True
    # integrity stays at 0 (monotonic drop).
    # role lands (valid enum value).
    # schemaless lands (unknown key = free write).
    assert engine.labels == {
        "integrity": "0",
        "role": "admin",
        "schemaless": "free",
    }
