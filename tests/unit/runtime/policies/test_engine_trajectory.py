"""
Tests for engine trajectory population (step 2 of
designs/LIVE_POLICIES.md).

The engine queries the conversation store on every
``evaluate()`` call and threads the last
``_TRAJECTORY_WINDOW`` items onto ``EvaluationContext.trajectory``
in chronological order. The prompt-policy builtin reads this
to produce situational classifier reason text; other
``FunctionPolicy`` callables may ignore it.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.entities.conversation import (
    MessageData,
    NewConversationItem,
)
from omnigent.policies.base import Policy
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runtime.policies.engine import _TRAJECTORY_WINDOW, PolicyEngine
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    Phase,
    PhaseSelector,
    PolicyAction,
    PolicySpec,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


class _CapturingPolicySpec(PolicySpec):
    """Plain spec used for the capturing policy below."""


class _CapturingPolicy(Policy):
    """
    Policy stub that records every ``EvaluationContext`` it sees.

    Used to assert the engine populated ``ctx.trajectory`` before
    dispatching. Returns ALLOW unconditionally so the engine's
    composition path is exercised end-to-end.
    """

    def __init__(self, spec: PolicySpec) -> None:
        self.spec = spec
        self.seen_contexts: list[EvaluationContext] = []

    async def evaluate(
        self,
        ctx: EvaluationContext,
        context: dict[str, Any],
    ) -> PolicyResult:
        self.seen_contexts.append(ctx)
        return PolicyResult(action=PolicyAction.ALLOW)


def _make_spec() -> PolicySpec:
    """Build a minimal PolicySpec that fires on tool_call."""
    return PolicySpec(
        name="trajectory-capture-test",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
    )


def _make_engine(
    conversation_store: SqlAlchemyConversationStore,
    policy: Policy,
    conversation_id: str,
) -> PolicyEngine:
    return PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=DEFAULT_ASK_TIMEOUT,
        conversation_id=conversation_id,
        initial_labels={},
        conversation_store=conversation_store,
    )


def _make_conversation(conversation_store: SqlAlchemyConversationStore) -> str:
    """Create an empty conversation row and return its store-assigned id."""
    return conversation_store.create_conversation().id


@pytest.mark.asyncio
async def test_trajectory_empty_for_new_conversation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Engine populates trajectory=[] for a brand-new conversation.

    If the engine returned ``None`` here, the formatter
    couldn't tell "no items yet" from "engine never populated."
    The list-not-None invariant is what the prompt template
    relies on to render the placeholder string.
    """
    conv_id = _make_conversation(conversation_store)
    spec = _make_spec()
    capturing = _CapturingPolicy(spec)
    engine = _make_engine(conversation_store, capturing, conv_id)

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "Read", "arguments": {"path": "x"}},
        tool_name="Read",
    )
    await engine.evaluate(ctx)

    # The capturing policy saw exactly one ctx — the engine's
    # trajectory-populated copy.
    assert len(capturing.seen_contexts) == 1
    seen = capturing.seen_contexts[0]
    # trajectory is a list (not None) — empty for fresh convos.
    assert seen.trajectory == []


@pytest.mark.asyncio
async def test_trajectory_returns_items_in_chronological_order(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Engine returns trajectory ordered oldest-first.

    The store query runs ``order='desc'`` to fetch the tail
    cheaply, but the engine reverses so the classifier reads
    items top-down (matches how a human reads a conversation).
    If the engine forgot to reverse, the classifier would see
    the most recent item FIRST — confusing temporal reasoning.
    """
    conv_id = _make_conversation(conversation_store)
    # Three items, appended in order. Each gets a higher
    # ``position`` than its predecessor, so chronological
    # order is "first-message", "second-message", "third-message".
    for text in ["first-message", "second-message", "third-message"]:
        conversation_store.append(
            conversation_id=conv_id,
            items=[
                NewConversationItem(
                    type="message",
                    response_id="resp_traj_test",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": text}],
                    ),
                ),
            ],
        )

    spec = _make_spec()
    capturing = _CapturingPolicy(spec)
    engine = _make_engine(conversation_store, capturing, conv_id)

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "Read", "arguments": {}},
        tool_name="Read",
    )
    await engine.evaluate(ctx)

    seen = capturing.seen_contexts[0]
    assert seen.trajectory is not None
    # Three items, oldest first. If reversed, this list would
    # start with "third-message" — fail loud.
    assert len(seen.trajectory) == 3
    texts = [
        item.data.content[0]["text"]
        for item in seen.trajectory
        if isinstance(item.data, MessageData)
    ]
    assert texts == ["first-message", "second-message", "third-message"]


@pytest.mark.asyncio
async def test_trajectory_caps_at_window_size(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Engine fetches at most ``_TRAJECTORY_WINDOW`` items.

    With more conversation items than the window allows, the
    engine returns ONLY the most recent ``_TRAJECTORY_WINDOW``
    (still chronological order). Pinning the cap prevents
    runaway prompt-cost on long conversations.
    """
    # The window cap is what the engine respects; appending
    # window+5 items proves it doesn't fetch unbounded.
    conv_id = _make_conversation(conversation_store)
    n_items = _TRAJECTORY_WINDOW + 5
    for i in range(n_items):
        conversation_store.append(
            conversation_id=conv_id,
            items=[
                NewConversationItem(
                    type="message",
                    response_id="resp_traj_test",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": f"msg-{i}"}],
                    ),
                ),
            ],
        )

    spec = _make_spec()
    capturing = _CapturingPolicy(spec)
    engine = _make_engine(conversation_store, capturing, conv_id)

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "Read", "arguments": {}},
        tool_name="Read",
    )
    await engine.evaluate(ctx)

    seen = capturing.seen_contexts[0]
    assert seen.trajectory is not None
    # Returned list size is exactly the window — more would
    # break the cost cap; fewer would mean we lost recent context.
    assert len(seen.trajectory) == _TRAJECTORY_WINDOW
    # And it's the MOST RECENT window items (msg-5 through msg-14
    # for a window of 10), not the first ones.
    last_text = seen.trajectory[-1].data.content[0]["text"]  # type: ignore[union-attr]
    assert last_text == f"msg-{n_items - 1}"
    first_text = seen.trajectory[0].data.content[0]["text"]  # type: ignore[union-attr]
    assert first_text == f"msg-{n_items - _TRAJECTORY_WINDOW}"


@pytest.mark.asyncio
async def test_caller_supplied_trajectory_is_overwritten(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Engine overwrites ctx.trajectory even if the caller pre-set it.

    The engine is the canonical source of trajectory — if a
    caller passed a stale list, the engine's fresh fetch wins.
    This protects against test contexts that hand-constructed
    a ctx with mock trajectory, then accidentally hit a real
    engine path; the engine should still query the live store.
    """
    conv_id = _make_conversation(conversation_store)
    conversation_store.append(
        conversation_id=conv_id,
        items=[
            NewConversationItem(
                type="message",
                response_id="resp_traj_test",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "real-store-msg"}],
                ),
            ),
        ],
    )

    spec = _make_spec()
    capturing = _CapturingPolicy(spec)
    engine = _make_engine(conversation_store, capturing, conv_id)

    # Caller passes ctx with a fake trajectory list — engine
    # must replace it, not preserve.
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "Read", "arguments": {}},
        tool_name="Read",
        trajectory=[],  # bogus pre-fill
    )
    await engine.evaluate(ctx)

    seen = capturing.seen_contexts[0]
    assert seen.trajectory is not None
    # Engine's query won; the bogus empty list was overwritten.
    assert len(seen.trajectory) == 1


def test_trajectory_window_constant_is_ten() -> None:
    """``_TRAJECTORY_WINDOW`` is exported and equals 10.

    Test pins the value so changing it requires updating both
    the engine and this test — preventing accidental bumps that
    silently increase classifier prompt cost.
    """
    assert _TRAJECTORY_WINDOW == 10
