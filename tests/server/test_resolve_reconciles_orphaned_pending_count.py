"""Answering a parked prompt reconciles a restart-orphaned pending count.

When a sub-agent's runner dies and a restart wipes the in-memory index, the
persisted ``pending_elicitation_count`` is orphaned and the "Needs response"
badge stays lit. ``_resolve_elicitation`` reconciles the persisted count to the
live count only when the bound runner is confirmed offline: a reachable runner,
and one merely live on another replica (``WRONG_REPLICA``), own the
authoritative count and are left untouched.
"""

from __future__ import annotations

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state
from omnigent.server.routes import sessions as S


@pytest.fixture
def _clean_index():
    """Isolate the module-global count-persist hook per test.

    The autouse ``_reset_elicitation_state`` fixture already clears the index;
    it does not touch the persist hook. A hook leaked from another test would
    bypass the per-test ``persist_pending_count`` patch, so save and restore it.
    """
    prior_hook = pending_elicitations.get_count_persist_hook()
    pending_elicitations.set_count_persist_hook(None)
    yield
    pending_elicitations.set_count_persist_hook(prior_hook)


def _request_event(elicitation_id: str) -> dict:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": {"message": "Approve running 'ls'?"},
    }


@pytest.mark.asyncio
async def test_offline_runner_resolve_clears_orphaned_persisted_count(_clean_index, monkeypatch):
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
async def test_offline_runner_resolve_persists_remaining_live_count(_clean_index, monkeypatch):
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

    await S._resolve_elicitation(sid, {"elicitation_id": answered, "action": "accept"}, None)

    assert (sid, 1) in persisted and persisted[-1] == (sid, 1), (
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


class _RaisingRouter:
    """Router whose resource lookup fails with a chosen absence code."""

    def __init__(self, code: ErrorCode) -> None:
        self._code = code

    def client_for_session_resources(self, session_id: str):
        raise OmnigentError("no local client", code=self._code)


@pytest.mark.asyncio
async def test_wrong_replica_runner_resolve_does_not_reconcile(_clean_index, monkeypatch):
    """A runner live on another replica (``WRONG_REPLICA``) is reachable — just
    not from here. Reconciling off this replica's empty index would clobber the
    authoritative count the tunnel-holding replica owns, so the resolve must NOT
    reconcile even though the local client lookup returns ``None``."""
    sid = "conv_wrong_replica"
    eid = "elicit_evaluate_44444444444444444444444444444444"

    # The local-replica forward cannot reach the remote runner (returns None),
    # yet the runner is alive elsewhere — the router reports WRONG_REPLICA.
    async def _no_local_client(session_id, runner_router, **kwargs):
        return None

    monkeypatch.setattr(S, "_get_runner_client", _no_local_client)

    persisted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_pending_count",
        lambda conv_id, count: persisted.append((conv_id, count)),
    )

    router = _RaisingRouter(ErrorCode.WRONG_REPLICA)
    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, router)

    assert persisted == [], (
        "a WRONG_REPLICA miss means the runner is live on another replica that "
        f"owns the count; this replica must not reconcile; got {persisted}"
    )


@pytest.mark.asyncio
async def test_offline_runner_via_router_resolve_reconciles(_clean_index, monkeypatch):
    """A ``RUNNER_UNAVAILABLE`` lookup confirms the runner is genuinely gone, so
    the orphaned-count reconcile fires through the router path too."""
    sid = "conv_router_offline"
    eid = "elicit_evaluate_55555555555555555555555555555555"

    async def _no_local_client(session_id, runner_router, **kwargs):
        return None

    monkeypatch.setattr(S, "_get_runner_client", _no_local_client)

    persisted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_pending_count",
        lambda conv_id, count: persisted.append((conv_id, count)),
    )

    router = _RaisingRouter(ErrorCode.RUNNER_UNAVAILABLE)
    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, router)

    assert (sid, 0) in persisted, (
        "a confirmed-offline runner (RUNNER_UNAVAILABLE) must reconcile the "
        f"persisted count to the live count (0) via the router path; got {persisted}"
    )


@pytest.mark.asyncio
async def test_non_routing_router_error_resolve_does_not_reconcile(_clean_index, monkeypatch):
    """Only ``RUNNER_UNAVAILABLE`` confirms the runner is gone. A non-routing
    ``OmnigentError`` (here ``NOT_FOUND``, as the resource lookup raises for a
    missing conversation) is inconclusive, so the resolve must not reconcile and
    risk clobbering the authoritative count."""
    sid = "conv_router_notfound"
    eid = "elicit_evaluate_88888888888888888888888888888888"

    async def _no_local_client(session_id, runner_router, **kwargs):
        return None

    monkeypatch.setattr(S, "_get_runner_client", _no_local_client)

    persisted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_pending_count",
        lambda conv_id, count: persisted.append((conv_id, count)),
    )

    router = _RaisingRouter(ErrorCode.NOT_FOUND)
    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, router)

    assert persisted == [], (
        "a non-routing OmnigentError is not proof the runner is offline, so the "
        f"reconcile must be skipped rather than clobber the count; got {persisted}"
    )


class _ExplodingRouter:
    """Router whose resource lookup fails with a non-routing error."""

    def client_for_session_resources(self, session_id: str):
        raise LookupError("host record missing")


@pytest.mark.asyncio
async def test_router_lookup_error_resolve_stays_best_effort(_clean_index, monkeypatch):
    """A non-routing lookup failure is not proof the runner is gone: the resolve
    must neither raise (the answer already forwarded) nor reconcile blindly."""
    sid = "conv_router_boom"
    eid = "elicit_evaluate_66666666666666666666666666666666"

    async def _no_local_client(session_id, runner_router, **kwargs):
        return None

    monkeypatch.setattr(S, "_get_runner_client", _no_local_client)

    persisted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session_live_state,
        "persist_pending_count",
        lambda conv_id, count: persisted.append((conv_id, count)),
    )

    router = _ExplodingRouter()
    # Must not raise even though the routing lookup blows up.
    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, router)

    assert persisted == [], (
        "a non-routing lookup failure is inconclusive, so the reconcile must be "
        f"skipped rather than clobber the count; got {persisted}"
    )


class _RoutedRouter:
    """Router that successfully resolves a client for the session's runner."""

    def client_for_session_resources(self, session_id: str):
        return object()


@pytest.mark.asyncio
async def test_routed_runner_resolve_does_not_reconcile(_clean_index, monkeypatch):
    """A router that resolves a client locally means the runner is reachable from
    this replica, so the resolve must leave the authoritative count untouched."""
    sid = "conv_routed_live"
    eid = "elicit_evaluate_77777777777777777777777777777777"

    class _FakeResponse:
        status_code = 202

    class _FakeClient:
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

    router = _RoutedRouter()
    await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, router)

    assert persisted == [], (
        "a successfully routed (reachable) runner must not trigger the offline "
        f"reconcile; the routed replica owns the count; got {persisted}"
    )
