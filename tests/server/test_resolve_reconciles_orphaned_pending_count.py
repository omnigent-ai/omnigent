"""Answering a parked prompt must clear a restart-orphaned pending count.

A sub-agent's transient runner can die (SIGTERM / exit 143) and never reconnect;
a server restart then wipes the in-memory pending-elicitation index while the
persisted ``pending_elicitation_count`` survives on the conversation row. The
per-runner resync in ``_on_runner_connect`` only fires when that runner comes
back, and the in-memory ``resolve`` early-returns on the empty index — so before
the fix the user's answer never decremented the persisted count and the sidebar
"Needs response" badge stayed lit forever.

``_resolve_elicitation`` now reconciles the persisted count to the authoritative
live count whenever no runner tunnel is reachable, so an answer to an orphaned
prompt finally clears the badge. A reachable runner is left untouched: its own
resolve on the tunnel-holding replica writes the authoritative count.
"""

from __future__ import annotations

import pytest

from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state
from omnigent.server.routes import sessions as S


@pytest.fixture
def _clean_index():
    """Isolate the module-global pending-elicitation index per test."""
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


def _request_event(elicitation_id: str) -> dict:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": {"message": "Approve running 'ls'?"},
    }


@pytest.mark.asyncio
async def test_offline_runner_resolve_clears_orphaned_persisted_count(
    _clean_index, monkeypatch
):
    """No reachable runner + an empty index (post-restart orphan) → the resolve
    reconciles the persisted count to 0 so the stuck badge finally clears."""
    sid = "conv_orphan_restart"
    eid = "elicit_evaluate_deadbeefdeadbeefdeadbeefdeadbeef"

    # Post-restart: the in-memory index is empty, so the answered id is not
    # tracked. The dead runner is unreachable.
    async def _no_runner(session_id, runner_router, **kwargs):
        return None

    monkeypatch.setattr(S, "_get_runner_client", _no_runner)

    persisted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_pending_count",
        lambda conv_id, count: persisted.append((conv_id, count)),
    )

    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)

    assert (sid, 0) in persisted, (
        "an offline-runner resolve must reconcile the persisted count to the "
        "live count (0), clearing a restart-orphaned 'Needs response' badge"
    )


@pytest.mark.asyncio
async def test_offline_runner_resolve_persists_remaining_live_count(
    _clean_index, monkeypatch
):
    """Reconcile writes the authoritative live count, not a blind zero: a still
    -tracked sibling prompt keeps the count at 1 after an unrelated resolve."""
    sid = "conv_orphan_two"
    answered = "elicit_evaluate_11111111111111111111111111111111"
    sibling = "elicit_evaluate_22222222222222222222222222222222"

    # One prompt is still live in the index; the answered id is an orphan
    # (never tracked here).
    pending_elicitations.record_publish(sid, _request_event(sibling))
    assert pending_elicitations.count_for(sid) == 1

    async def _no_runner(session_id, runner_router, **kwargs):
        return None

    monkeypatch.setattr(S, "_get_runner_client", _no_runner)

    persisted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_pending_count",
        lambda conv_id, count: persisted.append((conv_id, count)),
    )

    await S._resolve_elicitation(
        sid, {"elicitation_id": answered, "action": "accept"}, None
    )

    assert persisted[-1] == (sid, 1), (
        "reconcile must persist the authoritative live count (1, the still-live "
        f"sibling), not a blind zero; got {persisted}"
    )


@pytest.mark.asyncio
async def test_reachable_runner_resolve_does_not_reconcile(_clean_index, monkeypatch):
    """A reachable runner is left to its own tunnel-replica resolve — the resolve
    path must NOT reconcile (and risk clobbering) the persisted count."""
    sid = "conv_live_runner"
    eid = "elicit_evaluate_33333333333333333333333333333333"

    class _FakeResponse:
        status_code = 202

    class _FakeClient:  # a truthy, reachable runner client the forward can POST to
        async def post(self, *args, **kwargs):
            return _FakeResponse()

    async def _live_runner(session_id, runner_router, **kwargs):
        return _FakeClient()

    monkeypatch.setattr(S, "_get_runner_client", _live_runner)

    persisted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_pending_count",
        lambda conv_id, count: persisted.append((conv_id, count)),
    )

    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)

    assert persisted == [], (
        "a reachable runner must not trigger the offline reconcile — the "
        f"tunnel-holding replica owns the count; got {persisted}"
    )
