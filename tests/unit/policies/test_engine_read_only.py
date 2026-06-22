"""Tests for ``PolicyEngine.evaluate(read_only=True)`` bypass of persistence.

Verifies that when ``read_only=True`` is passed to ``evaluate``:

- ALLOW path: no ``apply_label_writes`` or ``apply_state_updates`` calls
  are made, but the returned :class:`PolicyResult` still carries the
  label writes and state updates that *would* have been applied.
- DENY path: same — no persistence, but the result is still correct.
- Default (``read_only=False``): persistence happens as before.

Uses a minimal stub store that records mutations so we can assert on
actual persistence calls rather than relying on mock call counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.entities import ConversationItem, PagedList
from omnigent.policies.base import Policy
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    PolicyAction,
    StateUpdate,
    StateUpdateAction,
)

# ── Stub store ────────────────────────────────────────────────────


@dataclass
class _StubConversationStore:
    """
    Minimal conversation store that records label and state mutations.

    Only implements the methods :class:`PolicyEngine` actually calls
    during ``evaluate``: ``set_labels``, ``set_session_state``, and
    ``list_items``. Other methods raise if called, to surface
    unexpected interactions.

    :param label_writes: Accumulated ``set_labels`` calls as
        ``(conversation_id, labels)`` tuples.
    :param state_writes: Accumulated ``set_session_state`` calls as
        ``(conversation_id, state)`` tuples.
    """

    label_writes: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    state_writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def set_labels(self, conversation_id: str, labels: dict[str, str]) -> None:
        """
        Record a label write.

        :param conversation_id: The conversation being written to.
        :param labels: Label key/value pairs.
        """
        self.label_writes.append((conversation_id, dict(labels)))

    def set_session_state(self, conversation_id: str, state: dict[str, Any]) -> None:
        """
        Record a session-state write.

        :param conversation_id: The conversation being written to.
        :param state: The full state snapshot being persisted.
        """
        self.state_writes.append((conversation_id, dict(state)))

    def list_items(
        self,
        conversation_id: str,
        *,
        limit: int = 20,
        order: str = "asc",
        **kwargs: Any,
    ) -> PagedList[ConversationItem]:
        """
        Return an empty page — the engine only needs this for trajectory.

        :param conversation_id: Ignored.
        :param limit: Ignored.
        :param order: Ignored.
        :param kwargs: Ignored.
        :returns: Empty page with no items.
        """
        del conversation_id, limit, order, kwargs
        return PagedList(data=[], has_more=False)


# ── Stub policy ───────────────────────────────────────────────────


@dataclass
class _StubPolicy(Policy):
    """
    A test policy that returns a preconfigured result.

    :param spec: The policy spec.
    :param result: The result to return from ``evaluate``.
    """

    spec: FunctionPolicySpec
    result: PolicyResult

    async def evaluate(
        self,
        ctx: EvaluationContext,
        context: dict[str, Any],
    ) -> PolicyResult:
        """
        Return the preconfigured result unchanged.

        :param ctx: Ignored.
        :param context: Ignored.
        :returns: The ``result`` this stub was constructed with.
        """
        return self.result


# ── Helpers ───────────────────────────────────────────────────────

CONV_ID = "conv_read_only_test"


def _make_engine(
    store: _StubConversationStore,
    policies: list[Policy],
) -> PolicyEngine:
    """
    Build a :class:`PolicyEngine` with the given store and policies.

    :param store: The stub store for recording mutations.
    :param policies: The policies the engine should evaluate.
    :returns: A ready-to-evaluate engine.
    """
    return PolicyEngine(
        policies=policies,
        label_defs={},
        ask_timeout=30,
        conversation_id=CONV_ID,
        initial_labels={},
        conversation_store=store,  # type: ignore[arg-type]
    )


def _allow_policy(
    name: str,
    *,
    set_labels: dict[str, str] | None = None,
    state_updates: list[StateUpdate] | None = None,
) -> _StubPolicy:
    """
    Build a stub policy that returns ALLOW with optional side effects.

    :param name: Policy name, e.g. ``"allow_with_labels"``.
    :param set_labels: Labels the policy wants to write.
    :param state_updates: State updates the policy wants to apply.
    :returns: A configured stub policy.
    """
    spec = FunctionPolicySpec(name=name, on=None)
    return _StubPolicy(
        spec=spec,
        result=PolicyResult(
            action=PolicyAction.ALLOW,
            reason=None,
            set_labels=set_labels,
            state_updates=state_updates,
        ),
    )


def _deny_policy(
    name: str,
    *,
    reason: str = "denied",
    set_labels: dict[str, str] | None = None,
    state_updates: list[StateUpdate] | None = None,
) -> _StubPolicy:
    """
    Build a stub policy that returns DENY with optional side effects.

    :param name: Policy name, e.g. ``"deny_with_labels"``.
    :param reason: Denial reason text.
    :param set_labels: Labels the policy wants to write.
    :param state_updates: State updates the policy wants to apply.
    :returns: A configured stub policy.
    """
    spec = FunctionPolicySpec(name=name, on=None)
    return _StubPolicy(
        spec=spec,
        result=PolicyResult(
            action=PolicyAction.DENY,
            reason=reason,
            set_labels=set_labels,
            state_updates=state_updates,
        ),
    )


def _tool_call_ctx() -> EvaluationContext:
    """
    Build a minimal TOOL_CALL evaluation context for tests.

    :returns: An :class:`EvaluationContext` for the ``TOOL_CALL`` phase.
    """
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "grep", "arguments": {}},
        tool_name="grep",
    )


# ── ALLOW path tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_allow_read_only_skips_persistence() -> None:
    """``read_only=True`` on ALLOW returns labels/state but does not persist."""
    store = _StubConversationStore()
    updates = [StateUpdate(key="counter", action=StateUpdateAction.SET, value=1)]
    engine = _make_engine(
        store,
        [_allow_policy("labeler", set_labels={"env": "prod"}, state_updates=updates)],
    )

    result = await engine.evaluate(_tool_call_ctx(), read_only=True)

    assert result.action == PolicyAction.ALLOW
    # Result carries what *would* have been written.
    assert result.set_labels == {"env": "prod"}
    assert result.state_updates is not None
    assert result.state_updates[0].key == "counter"
    # Store received NO mutations.
    assert store.label_writes == [], "read_only=True must not persist label writes"
    assert store.state_writes == [], "read_only=True must not persist state updates"
    # Engine hot cache is also unchanged.
    assert engine.labels == {}
    assert engine.session_state == {}


@pytest.mark.asyncio
async def test_evaluate_allow_default_persists() -> None:
    """Default ``read_only=False`` on ALLOW persists labels and state."""
    store = _StubConversationStore()
    updates = [StateUpdate(key="counter", action=StateUpdateAction.SET, value=1)]
    engine = _make_engine(
        store,
        [_allow_policy("labeler", set_labels={"env": "prod"}, state_updates=updates)],
    )

    result = await engine.evaluate(_tool_call_ctx())

    assert result.action == PolicyAction.ALLOW
    assert result.set_labels == {"env": "prod"}
    assert result.state_updates is not None
    assert result.state_updates[0].key == "counter"
    # Store received mutations.
    assert len(store.label_writes) == 1
    assert store.label_writes[0] == (CONV_ID, {"env": "prod"})
    assert len(store.state_writes) == 1
    assert store.state_writes[0][0] == CONV_ID
    assert store.state_writes[0][1]["counter"] == 1
    # Engine hot cache updated.
    assert engine.labels == {"env": "prod"}
    assert engine.session_state["counter"] == 1


# ── DENY path tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_deny_read_only_skips_persistence() -> None:
    """``read_only=True`` on DENY returns labels/state but does not persist."""
    store = _StubConversationStore()
    updates = [StateUpdate(key="blocked", action=StateUpdateAction.SET, value=True)]
    engine = _make_engine(
        store,
        [
            _allow_policy("tagger", set_labels={"taint": "high"}),
            _deny_policy(
                "blocker",
                set_labels={"blocked": "yes"},
                state_updates=updates,
            ),
        ],
    )

    result = await engine.evaluate(_tool_call_ctx(), read_only=True)

    assert result.action == PolicyAction.DENY
    assert result.deciding_policy == "blocker"
    assert result.reason == "denied"
    # Result carries accumulated labels from both policies.
    assert result.set_labels is not None
    assert "taint" in result.set_labels
    assert "blocked" in result.set_labels
    # Result carries accumulated state updates.
    assert result.state_updates is not None
    assert result.state_updates[0].key == "blocked"
    # Store received NO mutations.
    assert store.label_writes == [], "read_only=True must not persist label writes on DENY"
    assert store.state_writes == [], "read_only=True must not persist state updates on DENY"
    # Engine hot cache is also unchanged.
    assert engine.labels == {}
    assert engine.session_state == {}


@pytest.mark.asyncio
async def test_evaluate_deny_default_persists() -> None:
    """Default ``read_only=False`` on DENY persists accumulated labels/state."""
    store = _StubConversationStore()
    updates = [StateUpdate(key="blocked", action=StateUpdateAction.SET, value=True)]
    engine = _make_engine(
        store,
        [
            _allow_policy("tagger", set_labels={"taint": "high"}),
            _deny_policy(
                "blocker",
                set_labels={"blocked": "yes"},
                state_updates=updates,
            ),
        ],
    )

    result = await engine.evaluate(_tool_call_ctx())

    assert result.action == PolicyAction.DENY
    assert result.deciding_policy == "blocker"
    # Store received mutations from both the ALLOW predecessor and the DENY.
    assert len(store.label_writes) == 1
    written_labels = store.label_writes[0][1]
    assert written_labels["taint"] == "high"
    assert written_labels["blocked"] == "yes"
    assert len(store.state_writes) == 1
    assert store.state_writes[0][1]["blocked"] is True


# ── read_only=False explicit (not just default) ──────────────────


@pytest.mark.asyncio
async def test_evaluate_explicit_read_only_false_persists() -> None:
    """Explicit ``read_only=False`` behaves the same as the default."""
    store = _StubConversationStore()
    engine = _make_engine(
        store,
        [_allow_policy("labeler", set_labels={"env": "staging"})],
    )

    result = await engine.evaluate(_tool_call_ctx(), read_only=False)

    assert result.action == PolicyAction.ALLOW
    assert result.set_labels == {"env": "staging"}
    assert len(store.label_writes) == 1
    assert engine.labels == {"env": "staging"}
