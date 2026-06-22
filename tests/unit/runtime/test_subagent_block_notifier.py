"""
Unit tests for :mod:`omnigent.runtime.subagent_block_notifier`.

The notifier observes elicitation publish events and wakes a blocked
sub-agent's *immediate parent* once per distinct block, then re-arms
when the block resolves. The wake delivery itself is an injected
``WakeDispatch`` callback so this module stays free of HTTP / runner
knowledge — these tests exercise every branch of the observer logic
against a real :class:`SqlAlchemyConversationStore` (no mocks for the
parent-resolve lookup) and a controllable async wake callback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from omnigent.entities.conversation import Conversation
from omnigent.runtime import pending_elicitations, subagent_block_notifier
from omnigent.runtime.subagent_block_notifier import (
    SubagentBlockNotifier,
    _block_reason,
    _child_label,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


async def _instant_sleep(_seconds: float) -> None:
    """
    Drop-in for the notifier's ``_sleep`` retry backoff that returns at once.

    Patched in over :func:`omnigent.runtime.subagent_block_notifier._sleep`
    (the module's own helper, not the global ``asyncio.sleep``) so retry tests
    are fast and deterministic with no real wall-clock wait.

    :param _seconds: Ignored backoff duration.
    :returns: None.
    """
    return


def elicitation_armed(notifier: SubagentBlockNotifier, elicitation_id: str) -> bool:
    """
    Report whether ``elicitation_id``'s debounce arm is currently held.

    Read-only assertion accessor: tests drive state changes through the
    public ``observe`` / ``record_publish`` surface and use this only to
    *observe* the resulting arm, never to mutate it. The arm is the
    per-block dedupe slot — held after a confirmed wake, released after a
    resolve or an exhausted-failure.

    :param notifier: The notifier under test.
    :param elicitation_id: Correlation id whose arm to inspect.
    :returns: ``True`` if the id is currently armed (debounced), else
        ``False``.
    """
    with notifier._lock:
        return elicitation_id in notifier._notified


# Per-test @pytest.mark.asyncio rather than a module marker: the pure
# formatter-helper tests below are sync, and a blanket marker makes
# pytest-asyncio warn about non-async functions carrying it.


@dataclass
class _CapturedWake:
    """
    One captured ``wake_dispatch`` invocation from a notifier test.

    :param parent_id: Parent session id passed to the dispatch.
    :param child_id: ``child.id`` recorded at dispatch time so tests
        don't need to keep a separate handle to the conversation.
    :param notice: The pre-formatted ``[System: …]`` notice text.
    """

    parent_id: str
    child_id: str
    notice: str


class _RecordingDispatch:
    """
    ``WakeDispatch`` test double that records every call.

    Tests assert on :attr:`calls` to verify that the notifier woke the
    correct parent exactly once per block. Pure recording — no HTTP, no
    side effects, so the tests exercise the *observer* logic in
    isolation.

    The default call returns ``True`` (delivery confirmed), matching the
    production contract so the per-block debounce stays armed after a
    successful wake. Error-path behavior is configured per-instance.

    :param fail_with: Optional exception to raise on every call. ``None``
        records the call and returns ``True``. Used by error-path tests.
    """

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self.calls: list[_CapturedWake] = []
        self._fail_with = fail_with

    async def __call__(self, parent_id: str, child: Conversation, notice: str) -> bool:
        """
        Record a wake dispatch (or raise the configured exception).

        :param parent_id: Parent session id.
        :param child: Blocked child conversation.
        :param notice: Notice text the notifier formatted.
        :returns: ``True`` (delivery confirmed) unless configured to raise.
        """
        self.calls.append(
            _CapturedWake(
                parent_id=parent_id,
                child_id=child.id,
                notice=notice,
            )
        )
        if self._fail_with is not None:
            raise self._fail_with
        return True


class _FailThenSucceedDispatch:
    """
    ``WakeDispatch`` stub that fails the first delivery, then succeeds.

    A real stub (not ``MagicMock``) so an unexpected extra call surfaces
    in :attr:`calls` rather than being silently absorbed. Returns ``False``
    (delivery not confirmed) on the first invocation — modelling the
    parent's runner being briefly unroutable during a reconnect — and
    ``True`` on every invocation thereafter.

    :ivar calls: Every recorded invocation, in order.
    """

    def __init__(self) -> None:
        self.calls: list[_CapturedWake] = []

    async def __call__(self, parent_id: str, child: Conversation, notice: str) -> bool:
        """
        Record the call; return ``False`` the first time, ``True`` after.

        :param parent_id: Parent session id.
        :param child: Blocked child conversation.
        :param notice: Notice text the notifier formatted.
        :returns: ``False`` on the first call, ``True`` on later calls.
        """
        self.calls.append(_CapturedWake(parent_id=parent_id, child_id=child.id, notice=notice))
        return len(self.calls) > 1


@pytest_asyncio.fixture
async def conv_store(tmp_path: Path) -> AsyncIterator[SqlAlchemyConversationStore]:
    """
    Per-test SQLite-backed conversation store.

    A real store is used (not a mock) so the notifier exercises the
    same ``get_conversation`` path the Omnigent server uses; a test-only
    in-memory shim could mask a regression in the parent-walk lookup.

    :param tmp_path: Pytest-provided unique temp directory.
    :returns: Configured store backed by a fresh SQLite file.
    """
    db_path = tmp_path / "test.db"
    store = SqlAlchemyConversationStore(f"sqlite:///{db_path}")
    yield store


@pytest.fixture(autouse=True)
def _reset_pending_elicitations() -> None:
    """Drain the pending-elicitations index between tests."""
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.fixture(autouse=True)
def _instant_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Skip the escalation grace by default so wake tests stay fast.

    Tests that exercise the grace itself re-patch
    ``subagent_block_notifier._escalation_sleep`` with a gate they control.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)


def _request_event(elicitation_id: str, message: str | None = None) -> dict[str, Any]:
    """
    Build a minimal ``response.elicitation_request`` event dict.

    :param elicitation_id: Correlation id, e.g. ``"elicit_abc"``.
    :param message: Optional human-readable prompt to embed in
        ``params.message`` so tests assert on reason propagation.
    """
    event: dict[str, Any] = {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
    }
    if message is not None:
        event["params"] = {"mode": "form", "message": message}
    return event


def _resolved_event(elicitation_id: str) -> dict[str, Any]:
    """Build a ``response.elicitation_resolved`` event dict."""
    return {
        "type": "response.elicitation_resolved",
        "elicitation_id": elicitation_id,
    }


async def _wait_for_calls(
    dispatch: _RecordingDispatch, expected: int, timeout_s: float = 1.0
) -> None:
    """
    Spin until ``dispatch.calls`` has at least ``expected`` entries.

    The notifier hands work to the loop via
    ``asyncio.run_coroutine_threadsafe``, so the assertion can't be
    immediate after :meth:`observe`. ``asyncio.sleep(0)`` yields control
    once per iteration so scheduled coroutines run.

    :param dispatch: Recording dispatch to poll.
    :param expected: Minimum number of calls to wait for.
    :param timeout_s: Hard ceiling so a stuck test fails loudly rather
        than hanging forever, e.g. ``1.0``.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while len(dispatch.calls) < expected:
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(
                f"timed out waiting for {expected} wake dispatch(es); got {len(dispatch.calls)}"
            )
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_observe_wakes_immediate_parent_for_blocked_child(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    A child elicitation request triggers exactly one wake on its parent.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:auth-fix", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(
        child.id,
        _request_event("elicit_first", message="Codex wants to run 'date'"),
    )
    await _wait_for_calls(dispatch, expected=1)

    # Exactly one dispatch — the immediate parent is the wake target,
    # and the notice carries the agent label + prompt reason so the
    # parent agent can decide what to do without re-fetching.
    assert len(dispatch.calls) == 1
    call = dispatch.calls[0]
    assert call.parent_id == parent.id
    assert call.child_id == child.id
    assert "codex/auth-fix" in call.notice
    assert "Codex wants to run 'date'" in call.notice
    assert call.notice.startswith("[System:")


@pytest.mark.asyncio
async def test_observe_no_op_for_top_level_session(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    A top-level session's elicitation does not fire a wake.
    """
    top = conv_store.create_conversation(kind="default", title="root")
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(top.id, _request_event("elicit_top"))
    # Yield so any spurious scheduled wake can fire before we assert empty.
    # Without the parent_conversation_id gate this would record 1 call.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_observe_debounces_repeated_publishes_of_same_id(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    Re-publishing the same elicitation_id wakes the parent only once.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:dup", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, _request_event("elicit_dup"))
    notifier.observe(child.id, _request_event("elicit_dup"))
    notifier.observe(child.id, _request_event("elicit_dup"))
    await _wait_for_calls(dispatch, expected=1)
    # Yield a couple more times to give any duplicate dispatch a chance
    # to fire — if the debounce is missing, this surfaces it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # 1, not 3: the de-dupe is what stops a fan-out's worth of repeated
    # publishes (e.g. on a reconnect that re-emits the same id) from
    # spamming the parent's chat with identical wake notices.
    assert len(dispatch.calls) == 1


@pytest.mark.asyncio
async def test_observe_re_arms_after_resolved_event(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    Resolving the block lets a future block of the same id wake again.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:rearm", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, _request_event("elicit_one"))
    await _wait_for_calls(dispatch, expected=1)
    notifier.observe(child.id, _resolved_event("elicit_one"))
    # The arm drops synchronously in observe, so the re-block below
    # re-fires; the resolve also signals the first handler to send its
    # resolution notice.
    notifier.observe(child.id, _request_event("elicit_one"))
    await _wait_for_calls(dispatch, expected=3)

    # 3 = block wake + resolution notice for it + re-block wake. 2 would
    # mean the resolution notice was dropped; 1 would mean the
    # resolved-event branch missed and the re-block was debounced away.
    # The notice/wake order after the resolve is scheduling-dependent,
    # so assert by kind rather than position.
    assert len(dispatch.calls) == 3
    block_notices = [c for c in dispatch.calls if "blocked awaiting human approval" in c.notice]
    resolution_notices = [c for c in dispatch.calls if "has been resolved" in c.notice]
    assert len(block_notices) == 2
    assert len(resolution_notices) == 1


class _GatedEscalation:
    """
    Escalation-sleep stand-in the test opens explicitly.

    Patched over ``subagent_block_notifier._escalation_sleep`` so the
    grace window is a deterministic gate instead of wall-clock time:
    handlers park on :meth:`__call__` until the test calls
    :meth:`release`.

    :ivar entered: Count of handlers that reached the grace, so tests
        can assert the handler is parked before acting.
    """

    def __init__(self) -> None:
        self._gate = asyncio.Event()
        self.entered = 0

    async def __call__(self, _seconds: float) -> None:
        """
        Park until the test releases the gate.

        :param _seconds: Ignored grace duration.
        :returns: None.
        """
        self.entered += 1
        await self._gate.wait()

    def release(self) -> None:
        """
        Open the gate for every parked (and future) handler.

        :returns: None.
        """
        self._gate.set()


@pytest.mark.asyncio
async def test_resolve_during_escalation_grace_suppresses_wake(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A block answered within the escalation grace never wakes the parent.

    This is the attended-web case the grace exists for: the approval card
    is mirrored into the parent chat the moment the block publishes, so a
    human answering it promptly should leave the parent conversation
    untouched — no ``[System: …]`` block notice, no resolution notice.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="claude_code:quiz", parent_conversation_id=parent.id
    )
    gate = _GatedEscalation()
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", gate)
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, _request_event("elicit_attended"))
    # Wait until the handler is parked in the grace, then resolve.
    deadline = asyncio.get_event_loop().time() + 1.0
    while gate.entered < 1:
        assert asyncio.get_event_loop().time() < deadline, "handler never reached the grace"
        await asyncio.sleep(0)
    notifier.observe(child.id, _resolved_event("elicit_attended"))
    gate.release()
    # Drain so a wrongly-surviving wake would land before the assertion.
    for _ in range(10):
        await asyncio.sleep(0)

    # Zero dispatches: the post-grace arm re-check saw the resolve and
    # bailed. 1 here means the grace check regressed and attended users
    # get the redundant block notice again.
    assert dispatch.calls == []
    assert not elicitation_armed(notifier, "elicit_attended")


@pytest.mark.asyncio
async def test_wake_fires_only_after_escalation_grace(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An unanswered block wakes the parent only once the grace elapses.

    Pins both halves of the escalation contract: no dispatch while the
    grace is pending (the mirrored card is the surface), and the wake —
    pointing at that mirrored prompt — once it expires unanswered.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="claude_code:idle", parent_conversation_id=parent.id
    )
    gate = _GatedEscalation()
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", gate)
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, _request_event("elicit_idle", message="Run rm -rf?"))
    deadline = asyncio.get_event_loop().time() + 1.0
    while gate.entered < 1:
        assert asyncio.get_event_loop().time() < deadline, "handler never reached the grace"
        await asyncio.sleep(0)
    # Parked in the grace: nothing may have been dispatched yet.
    assert dispatch.calls == []

    gate.release()
    await _wait_for_calls(dispatch, expected=1)

    assert len(dispatch.calls) == 1
    call = dispatch.calls[0]
    assert call.parent_id == parent.id
    # The notice tells the parent the human-facing prompt already exists
    # (mirrored card) so it doesn't direct the user somewhere else.
    assert "mirrored into this conversation" in call.notice
    assert "Run rm -rf?" in call.notice


@pytest.mark.asyncio
async def test_resolution_notice_follows_delivered_wake(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    Resolving a block the parent was woken for sends a resolution notice.

    This is the dangling-parent bug: the parent narrates the block notice
    (offering to relay answers), the human answers the mirrored card
    directly, and without the follow-up the parent waits forever on a
    conversation thread nobody will continue.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="claude_code:geo", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, _request_event("elicit_geo"))
    await _wait_for_calls(dispatch, expected=1)
    notifier.observe(child.id, _resolved_event("elicit_geo"))
    await _wait_for_calls(dispatch, expected=2)

    # Second dispatch is the resolution notice, to the same parent. 1
    # call total means the resolve signal never reached the waiting
    # handler and the parent stays dangling on the block notice.
    assert len(dispatch.calls) == 2
    resolution = dispatch.calls[1]
    assert resolution.parent_id == parent.id
    assert "claude_code/geo" in resolution.notice
    assert "has been resolved" in resolution.notice
    assert "result will arrive in your inbox" in resolution.notice
    # Arm released: a later re-block of the same id can wake again.
    assert not elicitation_armed(notifier, "elicit_geo")


class _ResolveDuringDispatch:
    """
    Dispatch double that resolves the block while its wake is in flight.

    Models the human answering the mirrored card in the instant the block
    notice is being delivered — the tightest race against the resolution
    signal. Real stub (not MagicMock) so unexpected extra calls surface.

    :ivar calls: Every recorded invocation, in order.
    :ivar notifier: Set by the test after construction (the double needs
        the notifier to inject the resolve).
    :ivar child_session_id: Session id to publish the resolve on.
    :ivar elicitation_id: Correlation id to resolve.
    """

    def __init__(self) -> None:
        self.calls: list[_CapturedWake] = []
        self.notifier: SubagentBlockNotifier | None = None
        self.child_session_id: str | None = None
        self.elicitation_id: str | None = None

    async def __call__(self, parent_id: str, child: Conversation, notice: str) -> bool:
        """
        Record the call; on the first (block) delivery, inject the resolve.

        :param parent_id: Parent session id.
        :param child: Blocked child conversation.
        :param notice: Notice text the notifier formatted.
        :returns: ``True`` (delivery confirmed).
        """
        self.calls.append(_CapturedWake(parent_id=parent_id, child_id=child.id, notice=notice))
        if len(self.calls) == 1:
            assert self.notifier is not None
            assert self.child_session_id is not None
            assert self.elicitation_id is not None
            self.notifier.observe(self.child_session_id, _resolved_event(self.elicitation_id))
        return True


@pytest.mark.asyncio
async def test_resolve_racing_inflight_wake_still_sends_resolution_notice(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    A resolve landing while the block notice is mid-delivery is not lost.

    The handler registers its resolve signal BEFORE dispatching; if it
    registered after, a resolve arriving during the dispatch would find
    no signal and the delivered block notice would dangle forever.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:race-resolve", parent_conversation_id=parent.id
    )
    dispatch = _ResolveDuringDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    dispatch.notifier = notifier
    dispatch.child_session_id = child.id
    dispatch.elicitation_id = "elicit_midflight"

    notifier.observe(child.id, _request_event("elicit_midflight"))
    deadline = asyncio.get_event_loop().time() + 1.0
    while len(dispatch.calls) < 2:
        assert asyncio.get_event_loop().time() < deadline, (
            f"expected the resolution notice to follow the raced block wake; "
            f"got {len(dispatch.calls)} call(s)"
        )
        await asyncio.sleep(0)

    # Block notice + resolution notice, despite the resolve landing
    # inside the block dispatch itself.
    assert len(dispatch.calls) == 2
    assert "blocked awaiting human approval" in dispatch.calls[0].notice
    assert "has been resolved" in dispatch.calls[1].notice


@pytest.mark.asyncio
async def test_no_resolution_notice_after_failed_wake(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A block whose wake never delivered gets no resolution notice.

    The resolution notice exists to close the loop on a notice the parent
    actually received; sending one after an exhausted delivery failure
    would reference a block notice that never landed.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:fail", parent_conversation_id=parent.id
    )
    monkeypatch.setattr(subagent_block_notifier, "_sleep", _instant_sleep)
    dispatch = _RecordingDispatch(fail_with=RuntimeError("runner unroutable"))
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, _request_event("elicit_fail"))
    # 3 = 1 attempt + 2 retries, all raising; then the arm is released.
    await _wait_for_calls(dispatch, expected=3)
    deadline = asyncio.get_event_loop().time() + 1.0
    while elicitation_armed(notifier, "elicit_fail"):
        assert asyncio.get_event_loop().time() < deadline, "arm never released after failure"
        await asyncio.sleep(0)

    notifier.observe(child.id, _resolved_event("elicit_fail"))
    for _ in range(10):
        await asyncio.sleep(0)

    # Still exactly the 3 failed block attempts — no resolution notice
    # was dispatched for a wake that never reached the parent.
    assert len(dispatch.calls) == 3
    assert all("has been resolved" not in call.notice for call in dispatch.calls)


@pytest.mark.asyncio
async def test_observe_distinct_ids_each_fire_once(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    Two distinct blocks on the same child each wake the parent once.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:multi", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, _request_event("elicit_a", message="approve A"))
    notifier.observe(child.id, _request_event("elicit_b", message="approve B"))
    await _wait_for_calls(dispatch, expected=2)

    # Per-id debounce: a multi-step approval chain surfaces each step.
    # 1 instead of 2 would mean both ids shared one slot. Differing reason
    # text proves each prompt came through with its own payload.
    assert len(dispatch.calls) == 2
    notices = {call.notice for call in dispatch.calls}
    assert any("approve A" in n for n in notices)
    assert any("approve B" in n for n in notices)


@pytest.mark.asyncio
async def test_observe_wake_targets_only_recorded_parent_not_other_trees(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    Multi-user safety: a block wakes ONLY its own recorded parent.

    Sub-agents inherit their parent's owner, so resolving the wake
    target purely from ``parent_conversation_id`` keeps every wake
    inside one owner's tree. This builds two independent trees (standing
    in for two users) and proves a block in tree A dispatches solely to
    tree A's parent — never to tree B's parent or any other session. A
    regression that broadcast the prompt or mis-resolved the parent (the
    cross-user leak reviewers flagged on the stream-mirror
    approach) would surface here as an extra or wrong dispatch target.
    """
    parent_a = conv_store.create_conversation(kind="default", title="alice-root")
    child_a = conv_store.create_conversation(
        kind="sub_agent", title="codex:a", parent_conversation_id=parent_a.id
    )
    parent_b = conv_store.create_conversation(kind="default", title="bob-root")
    conv_store.create_conversation(
        kind="sub_agent", title="codex:b", parent_conversation_id=parent_b.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child_a.id, _request_event("elicit_tree_a"))
    await _wait_for_calls(dispatch, expected=1)
    # Yield so any stray second dispatch (e.g. a broadcast bug) surfaces
    # before we assert the single-target invariant.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Exactly one dispatch, to tree A's parent only. parent_b must never
    # be a target: the wake follows the recorded parent link and cannot
    # cross into another owner's tree.
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0].parent_id == parent_a.id
    assert all(call.parent_id != parent_b.id for call in dispatch.calls)


@pytest.mark.asyncio
async def test_observe_ignores_non_elicitation_events(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    Other event types on the publish path do not wake.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:noise", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe(child.id, {"type": "response.output_text.delta", "delta": "hi"})
    notifier.observe(child.id, {"type": "session.status", "status": "running"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # No wake for non-elicitation events — the observer is on the hot
    # publish path and must filter cleanly. A stray wake here would
    # spam the parent on every text delta.
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_observe_retries_then_releases_arm_when_dispatch_raises(
    conv_store: SqlAlchemyConversationStore,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A dispatch that always raises is retried, logged, and releases the arm.

    The publish path must not crash (the exception is caught inside the
    handler, not propagated back into ``observe``). A raising dispatch is a
    failed delivery: it is retried up to the bounded ceiling, each failure is
    logged with its traceback, and after exhaustion the per-block arm is
    released so the block is not silenced forever.
    """
    # No-op the retry backoff so the test is fast and deterministic (rule 14:
    # patch the module's own _sleep helper, never the global asyncio.sleep).
    monkeypatch.setattr(subagent_block_notifier, "_sleep", _instant_sleep, raising=True)
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:fail", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch(fail_with=RuntimeError("transport down"))
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    expected_attempts = 1 + subagent_block_notifier._WAKE_RETRIES
    with caplog.at_level(logging.WARNING, logger="omnigent.runtime.subagent_block_notifier"):
        notifier.observe(child.id, _request_event("elicit_fail"))
        await _wait_for_calls(dispatch, expected=expected_attempts)
        # Yield so the handler unwinds (final release + summary log) after the
        # last failing attempt completes.
        for _ in range(5):
            await asyncio.sleep(0)

    # Every attempt ran (1 initial + the retries): the exception was caught and
    # retried, never propagated back through observe() — if it had, it would
    # break every other consumer on the record_publish publish path.
    assert len(dispatch.calls) == expected_attempts
    # Each raising attempt was logged with its traceback — a non-transport
    # failure (a bug, a store error) must be debuggable, not silently dropped.
    raise_logs = [r for r in caplog.records if "wake dispatch raised" in r.getMessage()]
    assert len(raise_logs) == expected_attempts
    assert all(r.exc_info is not None for r in raise_logs)
    assert isinstance(raise_logs[0].exc_info[1], RuntimeError)
    # Arm released after exhaustion: an always-failing delivery must NOT leave
    # the block permanently debounced — a non-empty set here means a later
    # re-publish would be silently dropped (the exact regression this guards against).
    assert elicitation_armed(notifier, "elicit_fail") is False


@pytest.mark.asyncio
async def test_failed_dispatch_releases_arm_so_republish_re_fires(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed wake releases the arm; the next publish of that id re-dispatches.

    This is the regression guard. The parent's runner is briefly
    unroutable exactly when a child blocks (reconnect/relaunch), so the first
    wake dispatch returns ``False`` (not delivered). The notifier must then
    *release* the per-block debounce arm so that when the same elicitation is
    published again (the SSE chokepoint re-emits it, or the next poll re-fires
    it) the wake is re-attempted — and this time the runner is back, so the
    dispatch succeeds and the parent is finally told.

    Drives everything through the production publish vehicle
    (``record_publish`` → registered observer → notifier) and asserts only on
    observable state: the dispatch call log and the arm. With the fix reverted
    (arm armed before scheduling, never released on failure), the first failed
    dispatch leaves the arm stuck, the second ``record_publish`` is debounced
    away, ``dispatch.calls`` stays at length 1, and this test goes red.
    """
    # _WAKE_RETRIES = 0 isolates the *re-publish re-fire* path from the
    # in-handler retry (separately covered above): each publish makes exactly
    # one dispatch attempt, so call #1 is the failed block and call #2 is the
    # re-publish. _sleep is also stubbed defensively in case the ceiling moves.
    monkeypatch.setattr(subagent_block_notifier, "_WAKE_RETRIES", 0, raising=True)
    monkeypatch.setattr(subagent_block_notifier, "_sleep", _instant_sleep, raising=True)

    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:retry", parent_conversation_id=parent.id
    )
    dispatch = _FailThenSucceedDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    pending_elicitations.set_elicitation_observer(notifier.observe)

    event = _request_event("elicit_retry", message="Codex wants to run 'git fetch'")

    # First publish: runner unroutable → dispatch returns False.
    pending_elicitations.record_publish(child.id, event)
    await _wait_for_calls(dispatch, expected=1)
    # Yield so handle_request unwinds and releases the arm after the failure.
    for _ in range(5):
        await asyncio.sleep(0)

    # The failed delivery was attempted exactly once so far, against the parent.
    # A different count would mean the single-attempt assumption (_WAKE_RETRIES=0)
    # broke and the rest of the test no longer isolates the re-publish path.
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0].parent_id == parent.id
    # THE FIX: the arm was released after the failed dispatch. If it were still
    # held (the bug), the re-publish below would be debounced and never re-fire.
    assert elicitation_armed(notifier, "elicit_retry") is False

    # Re-publish the SAME elicitation id (runner has since rebound). Because the
    # arm was released, this is NOT debounced — it schedules a fresh wake.
    pending_elicitations.record_publish(child.id, event)
    await _wait_for_calls(dispatch, expected=2)
    for _ in range(5):
        await asyncio.sleep(0)

    # Re-dispatched: 2, not 1. A stuck arm (reverted fix) would debounce the
    # re-publish and leave this at 1 — the parent never learns of the block.
    assert len(dispatch.calls) == 2
    assert dispatch.calls[1].parent_id == parent.id
    # The redelivered notice still names the child + carries the approval
    # reason — proving the re-fire carried the real payload, not an empty wake.
    assert "codex/retry" in dispatch.calls[1].notice
    assert "git fetch" in dispatch.calls[1].notice
    # Arm now HELD: the second dispatch confirmed delivery (returned True), so
    # the success debounce is intact — a third publish of this id would be
    # suppressed. This proves the release is failure-specific, not unconditional.
    assert elicitation_armed(notifier, "elicit_retry") is True


@pytest.mark.asyncio
async def test_handle_request_skips_stale_wake_when_block_resolved(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    The handler re-checks the debounce slot and skips a now-stale wake.

    Models the race where a block resolves while the (off-loop) parent
    lookup is in flight: ``observe`` arms the slot on the request and clears
    it on the resolve — both synchronously on the publish path — and only
    then does the scheduled handler run. ``handle_request`` is awaited
    directly here (it is the notifier's public entry point for one request)
    so the assertion does not depend on loop-scheduling timing: with the
    slot already cleared, the handler must not wake the parent for a block
    that no longer exists.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:race", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )
    event = _request_event("elicit_race")

    # Arm the slot as a real request would, then clear it as its resolve
    # would — both synchronous, with no await between, so the resolve
    # deterministically lands before the handler proceeds.
    notifier.observe(child.id, event)
    notifier.observe(child.id, _resolved_event("elicit_race"))

    # Drive the handler to completion directly — deterministic, no reliance
    # on how many loop turns the auto-scheduled handler's lookup takes.
    await notifier.handle_request(child.id, event)
    # Drain the handler observe() auto-scheduled; it sees the same cleared
    # slot and likewise skips (so it never adds a call either).
    for _ in range(5):
        await asyncio.sleep(0)

    # Zero wakes: the handler's re-check saw the cleared slot and skipped.
    # Without that re-check the stale wake would fire — 1, not 0. (Re-arm
    # after resolve is covered by test_observe_re_arms_after_resolved_event.)
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_observe_no_op_when_child_conversation_missing(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    A request for an unknown conversation id is silently ignored.
    """
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    notifier.observe("conv_does_not_exist", _request_event("elicit_ghost"))
    # Yield a few times to surface any dispatch that might fire from a
    # mis-handled missing-conv case.
    for _ in range(4):
        await asyncio.sleep(0)

    # Cleanly absorbed: no wake, no exception. A KeyError or AttributeError
    # here would be a regression in the get_conversation None-guard.
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_observe_ignores_invalid_elicitation_id(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    A malformed elicitation_id is dropped without a wake.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:badid", parent_conversation_id=parent.id
    )
    dispatch = _RecordingDispatch()
    notifier = SubagentBlockNotifier(
        conversation_store=conv_store,
        wake_dispatch=dispatch,
        loop=asyncio.get_event_loop(),
    )

    # Empty id and non-string id are both invalid.
    notifier.observe(child.id, {"type": "response.elicitation_request", "elicitation_id": ""})
    notifier.observe(child.id, {"type": "response.elicitation_request", "elicitation_id": None})
    for _ in range(4):
        await asyncio.sleep(0)

    # Without a usable id, the debounce can never be cleared by a
    # later resolved event — so the safe path is not to wake at all.
    assert dispatch.calls == []


def test_block_reason_truncates_long_messages() -> None:
    """
    The reason echoed into the notice is bounded so a verbose prompt
    cannot bloat the parent's wake message.
    """
    long_msg = "x" * 500
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_long",
        "params": {"message": long_msg},
    }
    reason = _block_reason(event)
    assert reason is not None
    # 200-char ceiling per ``_REASON_MAX_CHARS``; ellipsis sentinel
    # signals that the reason was truncated rather than complete.
    assert len(reason) <= 200
    assert reason.endswith("…")


def test_block_reason_returns_none_for_missing_message() -> None:
    """
    An event with no ``params.message`` projects to ``None`` so the
    notice falls back to its message-less form.
    """
    assert _block_reason({"type": "response.elicitation_request"}) is None
    assert _block_reason({"params": {}}) is None
    assert _block_reason({"params": {"message": "   "}}) is None


def _make_conv(*, id: str, title: str | None) -> Conversation:
    """
    Build a minimal :class:`Conversation` for the label projector tests.

    The ``Conversation`` dataclass requires ``created_at`` /
    ``updated_at`` / ``root_conversation_id`` so this helper supplies
    plausible values for the ones the projector doesn't read.

    :param id: Conversation id, e.g. ``"conv_x"``.
    :param title: Title to project, e.g. ``"codex:auth-refactor"`` or
        ``None``.
    :returns: A :class:`Conversation`.
    """
    return Conversation(
        id=id,
        created_at=0,
        updated_at=0,
        root_conversation_id=id,
        title=title,
        kind="sub_agent",
        agent_id="ag_x",
        labels={},
    )


def test_child_label_handles_named_subagent_title() -> None:
    """
    A standard ``"<agent>:<title>"`` titles project to ``"<agent>/<title>"``.
    """
    conv = _make_conv(id="conv_x", title="codex:auth-refactor")
    assert _child_label(conv) == "codex/auth-refactor"


def test_child_label_falls_back_to_id_for_titleless_session() -> None:
    """
    A conversation with no title labels by id so the notice always
    names something the parent agent can act on.
    """
    conv = _make_conv(id="conv_naked", title="")
    assert _child_label(conv) == "conv_naked"
