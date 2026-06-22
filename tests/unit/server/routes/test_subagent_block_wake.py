"""
Server-side wake delivery for the sub-agent block notifier.

These tests exercise the *server* half of the feature —
:func:`omnigent.server.routes.sessions.configure_subagent_block_notifier`
plus the ``_wake_parent_for_blocked_child`` delivery it wires — driven
through the real
:func:`omnigent.runtime.pending_elicitations.record_publish` chokepoint
(the single point every elicitation publish funnels through). Only the
two boundaries the wake *reuses* are stubbed: ``_get_runner_client``
(runner resolution) and ``_dispatch_session_event_to_runner`` (the
existing, separately-tested forward). What is asserted is purely the new
code: that a child block resolves the parent, formats a ``[System: …]``
message, and delivers it to the parent session — and that it is a no-op
when no runner is bound (so it can never desync store/harness state).

A real :class:`SqlAlchemyConversationStore` is used (no mocked
``get_conversation``) so the parent-resolve path is the production one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from omnigent.entities.conversation import Conversation
from omnigent.runtime import pending_elicitations, subagent_block_notifier
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


async def _instant_sleep(_seconds: float) -> None:
    """
    No-op stand-in for the notifier's ``_sleep`` retry backoff.

    Patched over :func:`omnigent.runtime.subagent_block_notifier._sleep`
    (the module's own helper, not the global ``asyncio.sleep``) so the
    bounded wake retry adds no real wall-clock wait in these tests.

    :param _seconds: Ignored backoff duration.
    :returns: None.
    """
    return


pytestmark = pytest.mark.asyncio


@dataclass
class _DispatchCall:
    """
    One captured ``_dispatch_session_event_to_runner`` invocation.

    :param session_id: Session the event was dispatched to.
    :param text: The ``input_text`` body the wake injected.
    """

    session_id: str
    text: str


@pytest_asyncio.fixture
async def conv_store(tmp_path: Path) -> AsyncIterator[SqlAlchemyConversationStore]:
    """
    Per-test SQLite-backed conversation store.

    :param tmp_path: Pytest-provided unique temp directory.
    :returns: A store backed by a fresh SQLite file.
    """
    db_path = tmp_path / "test.db"
    store = SqlAlchemyConversationStore(f"sqlite:///{db_path}")
    yield store


@pytest.fixture(autouse=True)
def _reset_pending_elicitations() -> None:
    """Drain the index + clear any registered observer between tests."""
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.fixture(autouse=True)
def _instant_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Skip the escalation grace so wake delivery is immediate in tests.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(subagent_block_notifier, "_escalation_sleep", _instant_sleep)


def _request_event(elicitation_id: str, message: str) -> dict[str, Any]:
    """
    Build a ``response.elicitation_request`` event dict.

    :param elicitation_id: Correlation id, e.g. ``"elicit_demo"``.
    :param message: Human-readable prompt embedded in ``params.message``.
    """
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": {"mode": "form", "message": message},
    }


def _resolved_event(elicitation_id: str) -> dict[str, Any]:
    """
    Build a ``response.elicitation_resolved`` event dict.

    :param elicitation_id: Correlation id, e.g. ``"elicit_demo"``.
    """
    return {
        "type": "response.elicitation_resolved",
        "elicitation_id": elicitation_id,
    }


async def test_record_publish_delivers_wake_message_to_parent(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child block delivers a ``[System: …]`` wake to its parent session.

    Drives the full server wiring: ``record_publish`` → observer →
    notifier → ``_wake_parent_for_blocked_child`` → dispatch. Asserts
    the dispatch targeted the *parent* (not the child) with a notice
    naming the child and carrying the approval reason.
    """
    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:demo", parent_conversation_id=parent.id
    )

    delivered: list[_DispatchCall] = []
    fired = asyncio.Event()
    # Sentinel: only checked for non-None by the wake; the real dispatch
    # is stubbed so no transport is needed.
    sentinel_client = object()

    async def _fake_get_runner_client(session_id: str, runner_router: Any) -> object:
        return sentinel_client

    async def _record_dispatch(
        session_id: str,
        conv: Conversation,
        body: SessionEventInput,
        conversation_store: SqlAlchemyConversationStore,
        runner_client: object,
        *,
        agent_name: str | None = None,
        file_store: Any | None = None,
        artifact_store: Any | None = None,
        has_mcp_servers: bool = False,
        # The wake passes runner_router through; without it the stub
        # TypeErrors inside the notifier task and no wake lands.
        runner_router: Any | None = None,
    ) -> str:
        delivered.append(
            _DispatchCall(
                session_id=session_id,
                text=body.data["content"][0]["text"],
            )
        )
        fired.set()
        return "item_wake"

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    monkeypatch.setattr(sessions_module, "_dispatch_session_event_to_runner", _record_dispatch)

    uninstall = sessions_module.configure_subagent_block_notifier(conv_store, None)
    try:
        pending_elicitations.record_publish(
            child.id,
            _request_event("elicit_demo", "Codex wants to run 'git fetch'"),
        )
        # Deterministic: the recorder sets ``fired`` from inside the
        # dispatch, so this resolves exactly when the wake lands (the
        # timeout is only a stuck-test guard).
        await asyncio.wait_for(fired.wait(), timeout=2.0)

        # Exactly one wake, to the PARENT session (owner-scoped) — never the
        # child. A child target would mean the parent never learns of the block.
        assert len(delivered) == 1
        call = delivered[0]
        assert call.session_id == parent.id
        # The notice names the child + echoes the approval reason so the
        # parent agent can surface it without re-fetching the child.
        assert call.text.startswith("[System:")
        assert "codex/demo" in call.text
        assert "git fetch" in call.text

        # Resolving the block sends the woken parent a follow-up through
        # the same wiring, so it stops acting on the stale block notice.
        fired.clear()
        pending_elicitations.record_publish(child.id, _resolved_event("elicit_demo"))
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    finally:
        uninstall()

    # 2 = block wake + resolution notice. 1 means the resolve never
    # reached the waiting handler and the parent dangles on the notice.
    assert len(delivered) == 2
    resolution = delivered[1]
    assert resolution.session_id == parent.id
    assert "codex/demo" in resolution.text
    assert "has been resolved" in resolution.text


async def test_record_publish_no_wake_when_no_runner_bound(
    conv_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no runner bound to the parent, the wake is a no-op (but retried).

    A message event for an unbound parent must NOT be dispatched —
    forwarding one would desync conversation store and harness state. The
    wake resolves the runner, sees ``None``, logs, and reports failure so
    the notifier releases its debounce. Because an unbound parent is the
    transient-miss case the notifier retries, the runner lookup runs once
    per attempt (``1 + _WAKE_RETRIES``) — and no attempt dispatches.
    """
    # No-op the backoff so the bounded retry doesn't add real wall-clock
    # sleeps (rule 14: patch the module's _sleep, not global asyncio.sleep).
    monkeypatch.setattr(subagent_block_notifier, "_sleep", _instant_sleep, raising=True)

    parent = conv_store.create_conversation(kind="default", title="parent")
    child = conv_store.create_conversation(
        kind="sub_agent", title="codex:norunner", parent_conversation_id=parent.id
    )

    dispatched: list[str] = []
    lookups = 0
    expected_attempts = 1 + subagent_block_notifier._WAKE_RETRIES
    all_attempts_done = asyncio.Event()

    async def _no_runner_client(session_id: str, runner_router: Any) -> None:
        nonlocal lookups
        lookups += 1
        if lookups >= expected_attempts:
            all_attempts_done.set()
        return

    async def _record_dispatch(*args: Any, **kwargs: Any) -> None:
        dispatched.append("called")
        return

    monkeypatch.setattr(sessions_module, "_get_runner_client", _no_runner_client)
    monkeypatch.setattr(sessions_module, "_dispatch_session_event_to_runner", _record_dispatch)

    uninstall = sessions_module.configure_subagent_block_notifier(conv_store, None)
    try:
        pending_elicitations.record_publish(child.id, _request_event("elicit_norunner", "approve"))
        # Deterministic: the lookup stub sets the event on its final attempt,
        # so this resolves exactly when the retry loop exhausts (timeout is
        # only a stuck-test guard).
        await asyncio.wait_for(all_attempts_done.wait(), timeout=2.0)
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        uninstall()

    # No dispatch on any attempt: the unbound-parent branch dropped the wake
    # instead of forwarding an item the harness would never see. A non-empty
    # list would mean a None runner_client slipped past the guard.
    assert dispatched == []
    # The runner lookup ran once per bounded attempt — confirming the
    # server-layer retry actually re-tries the unroutable parent rather than
    # giving up after the first miss.
    assert lookups == expected_attempts
