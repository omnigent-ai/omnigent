"""Engine routing of the per-user daily cost-budget ASK approval.

The daily cost-budget policy emits its approved-checkpoint write under
the reserved ``USER_DAILY_ASK_APPROVED_STATE_KEY``. The engine must
route that to the session owner's ``user_daily_cost.ask_approved_usd``
(per user+day) — NOT the per-conversation ``session_state`` — so an
approval persists across the user's sessions. These tests exercise the
public ``PolicyEngine.apply_state_updates`` against a real store.
"""

from __future__ import annotations

import pytest

from omnigent.db.utils import now_epoch, utc_day
from omnigent.policies.builtins.cost import user_daily_cost_budget
from omnigent.policies.function import FunctionPolicy
from omnigent.policies.schema import USER_DAILY_ASK_APPROVED_STATE_KEY
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PhaseSelector,
    PolicyAction,
    StateUpdate,
    StateUpdateAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def _engine_with_owner(
    conversation_store: SqlAlchemyConversationStore, db_uri: str, owner: str
) -> tuple[PolicyEngine, str]:
    """
    Create a conversation owned by *owner* and a minimal engine on it.

    :param conversation_store: Store fixture.
    :param db_uri: DB URI (for the permission store).
    :param owner: The user to grant LEVEL_OWNER, e.g. ``"alice@example.com"``.
    :returns: ``(engine, conversation_id)``.
    """
    conv = conversation_store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(owner)
    perms.grant(owner, conv.id, 4)  # LEVEL_OWNER
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    return engine, conv.id


def test_daily_ask_key_routes_to_user_daily_store(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """The reserved daily key lands in user_daily_cost, not session_state."""
    owner = "alice@example.com"
    engine, _ = _engine_with_owner(conversation_store, db_uri, owner)

    engine.apply_state_updates(
        [
            StateUpdate(
                key=USER_DAILY_ASK_APPROVED_STATE_KEY, action=StateUpdateAction.SET, value=0.05
            )
        ]
    )

    today = utc_day(now_epoch())
    state = conversation_store.get_daily_cost_state(owner, today)
    # Routed to the per-user+day store...
    assert state["ask_approved_usd"] == pytest.approx(0.05)
    # ...and NOT leaked into the per-conversation session_state (which
    # would make the approval session-scoped, defeating the daily intent).
    assert USER_DAILY_ASK_APPROVED_STATE_KEY not in engine.session_state


def test_non_daily_key_still_goes_to_session_state(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """A normal state key keeps landing in session_state (regression guard)."""
    owner = "bob@example.com"
    engine, _ = _engine_with_owner(conversation_store, db_uri, owner)

    engine.apply_state_updates(
        [StateUpdate(key="call_count", action=StateUpdateAction.SET, value=3)]
    )

    # Normal key in session_state; daily store untouched (no row written).
    assert engine.session_state.get("call_count") == 3
    state = conversation_store.get_daily_cost_state(owner, utc_day(now_epoch()))
    assert state == {"cost_usd": 0.0, "ask_approved_usd": 0.0}


@pytest.mark.asyncio
async def test_approval_updates_in_memory_so_same_engine_does_not_reask(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> None:
    """After an approval, a 2nd evaluate on the SAME engine must not re-ASK.

    Regression guard for the in-memory snapshot update: the daily policy
    persists the approval to the store AND must refresh the engine's
    in-memory ``user_daily_cost`` (mirroring how the session policy keeps
    ``session_state`` current). Without that refresh, a second tool call
    evaluated by the same engine instance would inject the stale
    pre-approval snapshot and re-ASK the checkpoint just approved.
    """
    owner = "carol@example.com"
    conv = conversation_store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(owner)
    perms.grant(owner, conv.id, 4)  # LEVEL_OWNER

    policy = FunctionPolicy(
        FunctionPolicySpec(
            name="daily",
            on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
            function=FunctionRef(path="omnigent.policies.builtins.cost.user_daily_cost_budget"),
        ),
        user_daily_cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0]),
    )
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        # $3 today, nothing approved yet → first tool call crosses the $2 checkpoint.
        initial_user_daily_cost={"cost_usd": 3.0, "ask_approved_usd": 0.0},
        conversation_store=conversation_store,
    )
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "sys_os_shell", "arguments": {}},
        tool_name="sys_os_shell",
    )

    first = await engine.evaluate(ctx)
    assert first.action == PolicyAction.ASK  # crosses $2, not yet approved

    # Approve: applies the ASK's reserved daily state-update.
    engine.apply_state_updates(first.state_updates)

    second = await engine.evaluate(ctx)
    # WITHOUT the in-memory refresh this would ASK again (stale snapshot);
    # WITH it, the $2 checkpoint is approved → ALLOW.
    assert second.action == PolicyAction.ALLOW
