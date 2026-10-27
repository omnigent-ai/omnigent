"""A replica that hands a runner's tunnel to another replica leaves that runner's turns alone."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import cast

import pytest

from omnigent.entities import Conversation
from omnigent.server import shutdown_state
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store import ConversationStore

_RUNNER = "runner_0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_shutdown_state() -> Iterator[None]:
    shutdown_state.reset_for_tests()
    yield
    shutdown_state.reset_for_tests()


class _UnavailableStore:
    """Store whose reads fail, as on a draining replica losing its backends."""

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        raise AssertionError("a handed-off or failed decision must not be persisted here")

    def get_runner_liveness(self, conversation_id: str) -> None:
        raise ConnectionError("live-state backend unavailable")

    def get_conversation(self, conversation_id: str) -> None:
        raise ConnectionError("live-state backend unavailable")


def _event_attributes(
    caplog: pytest.LogCaptureFixture, event_name: str
) -> list[dict[str, object]]:
    return [
        cast("dict[str, object]", getattr(record, "attributes", {}))
        for record in caplog.records
        if getattr(record, "event_name", None) == event_name
    ]


@pytest.mark.parametrize("cached_status", [True, False], ids=["cached", "cold-cache"])
@pytest.mark.parametrize("handed_off", [True, False])
async def test_disconnect_decision_leaves_a_handed_off_turn_to_its_new_replica(
    caplog: pytest.LogCaptureFixture,
    handed_off: bool,
    cached_status: bool,
) -> None:
    """A mid-turn status, cached or from the snapshot after a failed read, fails only unmarked."""
    caplog.set_level(logging.INFO)
    session_id = "handoff-decision"
    snapshot = Conversation(
        id=session_id,
        root_conversation_id=session_id,
        created_at=0,
        updated_at=0,
        runner_id=_RUNNER,
        live_status="running",
    )
    if cached_status:
        sessions._session_status_cache[session_id] = "running"
    if handed_off:
        shutdown_state.mark_runner_handed_off(_RUNNER, reason="rebalance")
    try:
        requires_failure = await orchestration._runner_disconnect_requires_failure(
            session_id,
            cast(ConversationStore, _UnavailableStore()),
            origin="runner_offline_sweep",
            snapshot=snapshot,
        )
    finally:
        sessions._session_status_cache.pop(session_id, None)

    assert requires_failure is not handed_off
    [decision] = _event_attributes(caplog, "runner_disconnect_decision")
    assert decision["decision"] == ("handed_off" if handed_off else "failed_mid_turn")
    assert decision["status_source"] == ("cache" if cached_status else "snapshot")
    assert decision["runner_handoff_reason"] == ("rebalance" if handed_off else None)
    assert decision["server_shutting_down"] is False
