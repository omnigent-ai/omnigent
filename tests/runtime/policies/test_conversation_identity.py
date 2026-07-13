"""
Tests for conversation identity in the policy event context —
``conversation_id`` / ``root_conversation_id`` / ``sub_agent_name``.

Verifies:
- ``_build_event`` carries all three keys (``None``, not absent, when
  unpopulated) so callables can read them without ``KeyError``.
- ``_build_event`` passes populated values through unchanged.
- The engine injects its own ids and the dispatched sub-agent's name
  into ``event["context"]`` on every evaluation.
- A caller-supplied ``conversation_id`` on the context wins (test
  contexts), mirroring ``_inject_model``.
- Top-level sessions see ``root_conversation_id == conversation_id``
  and ``sub_agent_name is None``; sub-agent children see the root id
  and their name.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.function import FunctionPolicy, _build_event
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PhaseSelector,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _identity_capturing_policy(bucket: dict[str, Any]) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` that records the event context's
    conversation identity into *bucket* for assertion.

    :param bucket: Dict the captured ``conversation_id`` /
        ``root_conversation_id`` / ``sub_agent_name`` are written into.
    :returns: A capturing :class:`FunctionPolicy`.
    """

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        context = event["context"]
        bucket["conversation_id"] = context["conversation_id"]
        bucket["root_conversation_id"] = context["root_conversation_id"]
        bucket["sub_agent_name"] = context["sub_agent_name"]
        return {"result": "ALLOW"}

    spec = FunctionPolicySpec(
        name="capture_identity",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test.not.used"),
    )
    return FunctionPolicy(spec, _evaluate)


# ── _build_event carries the identity keys ──────────────────────────────────


def test_build_event_identity_keys_default_none() -> None:
    """
    ``_build_event`` includes all three identity keys as ``None`` when
    the context never populated them.

    What breaks if this fails: the keys are missing from the event
    dict, causing ``KeyError`` in policy callables that read
    ``event["context"]["conversation_id"]`` on paths that don't inject
    identity (the runner-local gate, test contexts).
    """
    event = _build_event(EvaluationContext(phase=Phase.REQUEST, content="hello"))
    context = event["context"]
    assert context["conversation_id"] is None
    assert context["root_conversation_id"] is None
    assert context["sub_agent_name"] is None


def test_build_event_identity_passthrough() -> None:
    """
    ``_build_event`` passes populated identity values through unchanged.

    What breaks if this fails: the engine's injected ids are dropped or
    renamed, so callables can't correlate evaluations to conversations.
    """
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content="hello",
        conversation_id="conv_child",
        root_conversation_id="conv_root",
        sub_agent_name="researcher",
    )
    context = _build_event(ctx)["context"]
    assert context["conversation_id"] == "conv_child"
    assert context["root_conversation_id"] == "conv_root"
    assert context["sub_agent_name"] == "researcher"


# ── Engine injection ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_injects_identity_for_top_level_session(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The engine injects its ``conversation_id`` into the event context;
    a top-level session's root equals its own id and has no
    ``sub_agent_name``.

    What breaks if this fails: policies keying session-scoped state or
    telemetry by conversation have no identity to key on and must fall
    back to fragile label conventions.
    """
    conv = conversation_store.create_conversation()
    bucket: dict[str, Any] = {}
    engine = PolicyEngine(
        policies=[_identity_capturing_policy(bucket)],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))
    assert bucket["conversation_id"] == conv.id
    assert bucket["root_conversation_id"] == conv.id
    assert bucket["sub_agent_name"] is None


@pytest.mark.asyncio
async def test_engine_injects_identity_for_sub_agent_child(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A sub-agent child's evaluations carry the child id, the tree root,
    and the dispatched sub-agent's name.

    What breaks if this fails: policies cannot distinguish child
    evaluations from top-level ones (``root_conversation_id`` equals
    ``conversation_id``), so per-child attribution and child-aware
    budgets silently degrade.
    """
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=root.id,
        sub_agent_name="researcher",
    )
    bucket: dict[str, Any] = {}
    engine = PolicyEngine(
        policies=[_identity_capturing_policy(bucket)],
        label_defs={},
        ask_timeout=30,
        conversation_id=child.id,
        initial_labels={},
        conversation_store=conversation_store,
        root_conversation_id=root.id,
        sub_agent_name="researcher",
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))
    assert bucket["conversation_id"] == child.id
    assert bucket["root_conversation_id"] == root.id
    assert bucket["sub_agent_name"] == "researcher"


@pytest.mark.asyncio
async def test_caller_supplied_conversation_id_wins(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A context that already carries a ``conversation_id`` is not
    overwritten by the engine (mirrors ``_inject_model``'s
    caller-wins rule).

    What breaks if this fails: test contexts and future callers that
    resolve identity from a fresher source get silently clobbered by
    the engine's snapshot.
    """
    conv = conversation_store.create_conversation()
    bucket: dict[str, Any] = {}
    engine = PolicyEngine(
        policies=[_identity_capturing_policy(bucket)],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    await engine.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="hi", conversation_id="conv_preset")
    )
    assert bucket["conversation_id"] == "conv_preset"
