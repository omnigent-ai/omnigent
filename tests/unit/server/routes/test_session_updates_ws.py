"""Tests for the ``WS /v1/sessions/updates`` push stream.

The endpoint replaces the web app's HTTP poll of ``GET /v1/sessions``:
a client sends the session ids it's watching and the server pushes a
``snapshot`` followed by ``changed`` / ``removed`` deltas as those
sessions change. These tests drive the real route (no mocks of the
store or auth) against file-backed SQLite stores, mutating persisted
conversation state to trigger deltas, and assert cross-user isolation.

The per-connection rescan interval is monkeypatched down so the
interval-driven ``changed`` / ``removed`` frames arrive promptly
instead of after the production 4 s cadence.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import omnigent.server.routes.sessions as sessions_routes
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.sessions import SessionLiveness, create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


class _NoIdentityAuthProvider:
    """Auth provider whose handshake yields no identity.

    Exercises the updates-stream's reject-when-unauthenticated gate
    deterministically — unlike header mode, which falls back to the
    reserved ``"local"`` user and would never return ``None``.
    """

    def get_user_id(self, request: object) -> None:
        """Always return ``None`` (no authenticated identity)."""
        del request
        return


@pytest.fixture
def fast_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Shrink the per-connection rescan interval so interval-driven
    deltas (``changed`` / ``removed``) land in well under a second.

    Patches the module-level constants the ticker reads at sleep time;
    these are plain config values, not ``asyncio`` singletons, so the
    patch is local and safe. The heartbeat interval is also shrunk so
    idle-only tests receive heartbeats promptly instead of waiting the
    production 30 s.
    """
    monkeypatch.setattr(sessions_routes, "_SESSION_UPDATES_RESCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(sessions_routes, "_SESSION_UPDATES_HEARTBEAT_INTERVAL_S", 0.1)


@pytest.fixture
def stores(
    db_uri: str,
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore]:
    """Real file-backed stores so writes from the test thread are visible
    to the WS handler thread."""
    return (
        SqlAlchemyConversationStore(db_uri),
        SqlAlchemyAgentStore(db_uri),
        SqlAlchemyPermissionStore(db_uri),
    )


@pytest.fixture
def liveness_state() -> dict[str, SessionLiveness]:
    """Mutable liveness map the test can flip mid-connection to drive a
    ``runner_online`` / ``host_online`` change through the stream. Ids
    absent from the map default to ``runner_online=True``,
    ``host_online=None`` (matching the server's missing-row terminal)."""
    return {}


@pytest.fixture
def comment_store(db_uri: str) -> SqlAlchemyCommentStore:
    """Real file-backed comment store so comment writes from the test
    thread are visible to the WS handler thread, mirroring ``stores``."""
    return SqlAlchemyCommentStore(db_uri)


@pytest.fixture
def app(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
    liveness_state: dict[str, SessionLiveness],
    comment_store: SqlAlchemyCommentStore,
) -> FastAPI:
    """Minimal app mounting only the sessions router, with header-based
    auth and a real permission store — the surface the updates stream
    actually exercises. ``liveness_lookup`` reads the mutable
    ``liveness_state`` (default runner online, no host) so tests control
    liveness."""
    conversation_store, agent_store, permission_store = stores

    def _liveness_lookup(ids: list[str]) -> dict[str, SessionLiveness]:
        return {
            sid: liveness_state.get(sid, SessionLiveness(runner_online=True, host_online=None))
            for sid in ids
        }

    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permission_store,
            liveness_lookup=_liveness_lookup,
            comment_store=comment_store,
        ),
        prefix="/v1",
    )
    return app


def _seed_session(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
    *,
    owner: str,
    title: str,
) -> str:
    """Create a session-shaped conversation (non-null ``agent_id``) owned
    by ``owner`` and return its id."""
    conversation_store, agent_store, permission_store = stores
    # The conversations.agent_id FK requires a real agent row; create one
    # idempotently (the same agent backs every seeded session).
    if agent_store.get("ag_test") is None:
        agent_store.create(
            agent_id="ag_test",
            name="test-agent",
            bundle_location="ag_test/bundle",
        )
    conv = conversation_store.create_conversation(title=title, agent_id="ag_test")
    permission_store.ensure_user(owner)
    permission_store.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _recv_until(ws: object, wanted: set[str], *, max_frames: int = 50) -> dict[str, object]:
    """
    Read frames until one whose ``type`` is in ``wanted`` arrives.

    Heartbeats and snapshots that aren't being awaited are skipped so a
    test can target a specific delta frame without depending on cadence.

    :param ws: The connected test WebSocket.
    :param wanted: Frame ``type`` values to stop on, e.g. ``{"changed"}``.
    :param max_frames: Safety bound so a missing frame fails fast rather
        than hanging.
    :returns: The first matching frame as a parsed dict.
    """
    for _ in range(max_frames):
        frame = json.loads(ws.receive_text())  # type: ignore[attr-defined]
        if frame.get("type") in wanted:
            return frame
    raise AssertionError(f"no frame in {wanted} after {max_frames} frames")


def test_watch_returns_snapshot_of_accessible_sessions(app: FastAPI, stores) -> None:
    """A ``watch`` for owned ids returns a snapshot containing exactly
    those sessions, with their persisted titles."""
    s1 = _seed_session(stores, owner=ALICE, title="first")
    s2 = _seed_session(stores, owner=ALICE, title="second")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1, s2]}))
        snapshot = _recv_until(ws, {"snapshot"})
        items = {item["id"]: item for item in snapshot["items"]}  # type: ignore[index]
        # Both watched, owned sessions are present — proves the snapshot
        # reads real conversation rows for exactly the watched ids.
        assert set(items) == {s1, s2}
        assert items[s1]["title"] == "first"
        assert items[s2]["title"] == "second"
        # No relay has reported, so the relay-fed status cache misses and
        # the list status presents as idle.
        assert items[s1]["status"] == "idle"
        # Both liveness fields are folded into the payload (defaults:
        # runner online, no host) so the client can drop its /health poll
        # for these sessions. A regression that stopped applying host_online
        # would drop the key (stream dumps full rows, so it must be present).
        assert items[s1]["runner_online"] is True
        assert items[s1]["host_online"] is None


def test_child_busy_rollup_flows_through_updates_stream(
    app: FastAPI,
    stores,
    fast_rescan: None,
) -> None:
    """
    A watched parent row reflects direct child sub-agent busy status.

    The sidebar relies on this WebSocket stream after the initial list
    fetch, so the stream's shared list-item builder must pass the same
    direct child ids as ``GET /v1/sessions``. This seeds a real child
    conversation and the relay-fed status cache, then proves both the
    initial snapshot and a later diff frame carry the rolled-up parent
    status.
    """
    conversation_store = stores[0]
    parent_id = _seed_session(stores, owner=ALICE, title="parent")
    child = conversation_store.create_conversation(
        kind="sub_agent",
        title="coder:auth",
        parent_conversation_id=parent_id,
        agent_id="ag_test",
    )
    sessions_routes._session_status_cache.pop(parent_id, None)
    sessions_routes._session_status_cache[child.id] = "waiting"
    try:
        with TestClient(app).websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws:
            ws.send_text(json.dumps({"type": "watch", "session_ids": [parent_id]}))
            snapshot = _recv_until(ws, {"snapshot"})
            snapshot_items = {item["id"]: item for item in snapshot["items"]}  # type: ignore[index]
            assert snapshot_items[parent_id]["status"] == "running"

            sessions_routes._session_status_cache[child.id] = "idle"
            changed = _recv_until(ws, {"changed"})
            changed_items = {item["id"]: item for item in changed["items"]}  # type: ignore[index]
            assert changed_items[parent_id]["status"] == "idle"
    finally:
        sessions_routes._session_status_cache.pop(parent_id, None)
        sessions_routes._session_status_cache.pop(child.id, None)


def test_runner_offline_pushes_changed_frame(
    app: FastAPI, stores, liveness_state: dict[str, SessionLiveness], fast_rescan: None
) -> None:
    """Flipping a watched session's runner to offline (host still up) pushes
    a ``changed`` frame carrying ``runner_online: false`` and
    ``host_online: true`` — this is the runner-down-but-host-alive state the
    open view uses, and it lets the client retire its /health poll."""
    s1 = _seed_session(stores, owner=ALICE, title="live")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        snapshot = _recv_until(ws, {"snapshot"})
        assert snapshot["items"][0]["runner_online"] is True  # type: ignore[index]
        # Runner goes offline but the host stays alive; the next rescan must
        # diff both fields and push them. A broken fold (a field missing or
        # not diffed) would yield only heartbeats and hang this wait.
        liveness_state[s1] = SessionLiveness(runner_online=False, host_online=True)
        changed = _recv_until(ws, {"changed"})
        items = {item["id"]: item for item in changed["items"]}  # type: ignore[index]
        # Strict runner_online flipped to False while host_online reports the
        # host is still reachable — exactly the pair that distinguishes "wake
        # the runner with a message" from "host offline, must reconnect".
        assert items[s1]["runner_online"] is False
        assert items[s1]["host_online"] is True


def test_list_sessions_omits_liveness_fields(
    app: FastAPI, stores, liveness_state: dict[str, SessionLiveness]
) -> None:
    """``GET /v1/sessions`` does NOT compute per-item liveness.

    The list deliberately skips ``runner_online`` / ``host_online``: no
    list consumer reads them (the sidebar no longer surfaces connection
    state), and the open-session view sources liveness from the
    single-session snapshot, the WS updates stream, and the ``/health``
    poll. Skipping it here drops the session-connectivity and hosts-table
    queries from every list call. The updates stream still carries the
    fields (see the changed-frame tests). Even with a liveness state set
    for the session, the list must not surface it."""
    s1 = _seed_session(stores, owner=ALICE, title="live")
    liveness_state[s1] = SessionLiveness(runner_online=False, host_online=True)

    resp = TestClient(app).get("/v1/sessions", headers={"X-Forwarded-Email": ALICE})

    assert resp.status_code == 200
    items = {item["id"]: item for item in resp.json()["data"]}
    # The list uses exclude_none; both keys are absent because the list
    # never computes liveness. Re-adding the lookup here would reintroduce
    # the per-list connectivity + hosts queries this change removed.
    assert "runner_online" not in items[s1]
    assert "host_online" not in items[s1]


def test_title_change_pushes_changed_frame(app: FastAPI, stores, fast_rescan: None) -> None:
    """Mutating a watched session's persisted title makes the server push
    a ``changed`` frame carrying the new value."""
    conversation_store = stores[0]
    s1 = _seed_session(stores, owner=ALICE, title="before")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        _recv_until(ws, {"snapshot"})
        # Mutate persisted state from the test thread; the handler's next
        # rescan must observe it and diff it against the snapshot baseline.
        conversation_store.update_conversation(s1, title="after")
        changed = _recv_until(ws, {"changed"})
        items = {item["id"]: item for item in changed["items"]}  # type: ignore[index]
        # The changed frame carries the new title — proves the diff fired
        # on the real field change, not a spurious or stale value.
        assert items[s1]["title"] == "after"


def test_cleared_field_pushes_explicit_null(app: FastAPI, stores, fast_rescan: None) -> None:
    """Clearing a previously-set nullable field (a runner unbind nulling
    ``runner_id``) pushes a ``changed`` frame that carries the key as an
    explicit ``null``.

    The stream dumps full rows (NOT ``exclude_none``) precisely so a
    non-null → null transition arrives as an explicit ``null`` the client can
    overlay-clear, rather than a dropped key that would leave the stale value.
    If the stream regressed to ``exclude_none``, ``runner_id`` would be absent
    from the frame and this test's ``in`` assertion fails.
    """
    conversation_store = stores[0]
    s1 = _seed_session(stores, owner=ALICE, title="bound")
    # Bind a runner first so the snapshot baseline carries runner_id; the
    # clear below is then a genuine non-null → null transition.
    assert conversation_store.set_runner_id(s1, "rnr_test") is True
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        snapshot = _recv_until(ws, {"snapshot"})
        # Baseline carries the bound runner — the value the clear must reverse.
        assert snapshot["items"][0]["runner_id"] == "rnr_test"  # type: ignore[index]
        conversation_store.clear_runner_id(s1)
        changed = _recv_until(ws, {"changed"})
        item = {i["id"]: i for i in changed["items"]}[s1]  # type: ignore[index]
        # The key is present AND explicitly null. Presence proves the stream
        # dumps full rows (exclude_none would omit it); the None value proves
        # the client can overlay-clear the stale runner_id.
        assert "runner_id" in item
        assert item["runner_id"] is None


def test_no_change_emits_no_changed_frame(app: FastAPI, stores, fast_rescan: None) -> None:
    """An idle watched session produces no ``changed`` frames — only
    heartbeats — so a static list generates no diff traffic."""
    s1 = _seed_session(stores, owner=ALICE, title="static")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        _recv_until(ws, {"snapshot"})
        # Several rescans elapse (interval 0.05 s); none should diff since
        # nothing changed. Any "changed"/"removed" here means the differ
        # is emitting on unchanged state.
        for _ in range(5):
            frame = json.loads(ws.receive_text())
            assert frame["type"] == "heartbeat", (
                f"expected only heartbeats for an unchanged session, got {frame['type']}"
            )


def test_delete_pushes_removed_frame(app: FastAPI, stores, fast_rescan: None) -> None:
    """Deleting a watched session makes the server push a ``removed``
    frame for its id (the row no longer resolves)."""
    permission_store = stores[2]
    s1 = _seed_session(stores, owner=ALICE, title="doomed")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        _recv_until(ws, {"snapshot"})
        # Drop access so the next rescan can no longer resolve the id for
        # this user — the list-stream's definition of "removed".
        permission_store.revoke(ALICE, s1)
        removed = _recv_until(ws, {"removed"})
        # The removed frame names exactly the now-inaccessible id.
        assert removed["ids"] == [s1]


def test_other_users_session_is_not_visible(app: FastAPI, stores, fast_rescan: None) -> None:
    """Bob watching Alice's session never receives it — neither in the
    snapshot nor in any later frame (cross-user isolation, W-series)."""
    s_alice = _seed_session(stores, owner=ALICE, title="alice-only")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": BOB}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s_alice]}))
        snapshot = _recv_until(ws, {"snapshot"})
        # Bob has no grant on Alice's session → the snapshot omits it.
        assert snapshot["items"] == []
        # And it must not leak via a later changed frame when Alice mutates
        # her own session.
        stores[0].update_conversation(s_alice, title="changed-by-alice")
        for _ in range(5):
            frame = json.loads(ws.receive_text())
            assert frame["type"] == "heartbeat", (
                f"Bob received a {frame['type']} frame for a session he "
                f"cannot access — cross-user leak"
            )


def test_unauthenticated_connection_is_rejected(stores) -> None:
    """With permissions enabled, a socket whose handshake yields no
    identity is closed at the handshake (policy violation) before any
    session data is read."""
    conversation_store, agent_store, permission_store = stores
    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            auth_provider=_NoIdentityAuthProvider(),  # type: ignore[arg-type]
            permission_store=permission_store,
        ),
        prefix="/v1",
    )
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect("/v1/sessions/updates"):
            pass
    # 1008 = WS_1008_POLICY_VIOLATION — the auth gate fired before accept.
    assert exc_info.value.code == 1008


def test_watch_set_truncated_at_cap(
    app: FastAPI,
    stores,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A watch-set larger than the cap is truncated to the cap, and the
    drop is logged. Ids past the cap never appear in the snapshot — they
    fall back to the client's list poll, so the server must surface the
    truncation rather than silently shrinking the set."""
    # Shrink the cap to 2 so three seeded sessions exceed it without
    # seeding the production 500.
    monkeypatch.setattr(sessions_routes, "_SESSION_UPDATES_MAX_WATCHED", 2)
    s1 = _seed_session(stores, owner=ALICE, title="one")
    s2 = _seed_session(stores, owner=ALICE, title="two")
    s3 = _seed_session(stores, owner=ALICE, title="three")
    with caplog.at_level(logging.WARNING, logger="omnigent.server.routes.sessions"):
        with TestClient(app).websocket_connect(
            "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
        ) as ws:
            # Send all three in order; the reader keeps the first two
            # (preserving order) and drops the third.
            ws.send_text(json.dumps({"type": "watch", "session_ids": [s1, s2, s3]}))
            snapshot = _recv_until(ws, {"snapshot"})
            ids = {item["id"] for item in snapshot["items"]}  # type: ignore[index]
            # Exactly the first two watched ids resolve; the third was
            # truncated before the snapshot fetch ever saw it. A regression
            # that dropped the cap would let all three through here.
            assert ids == {s1, s2}
            assert len(snapshot["items"]) == 2  # type: ignore[arg-type]
    # The truncation is logged with the distinct-id count so an oversized
    # watch-set is diagnosable. Absence here means the drop went silent.
    assert any("watch-set truncated" in record.getMessage() for record in caplog.records), (
        "expected a watch-set-truncation warning, got none"
    )


def test_transient_store_error_does_not_kill_stream(
    app: FastAPI,
    stores,
    fast_rescan: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store read that raises during one rescan tick is logged and
    skipped — the stream stays open and a later real change still pushes a
    ``changed`` frame. Without the ticker's guard, the raised exception
    would tear the connection down and force a reconnect + re-snapshot."""
    conversation_store = stores[0]
    s1 = _seed_session(stores, owner=ALICE, title="before")
    real_get = conversation_store.get_conversations
    calls = {"n": 0}

    def flaky_get(ids: list[str]) -> dict[str, object]:
        """Succeed on the connect snapshot, fail the first rescan tick,
        then recover — a transient blip, not a permanent outage."""
        calls["n"] += 1
        # Call 1 is the snapshot (must succeed so the client gets a
        # baseline). Call 2 is the first ticker rescan — fail it.
        if calls["n"] == 2:
            raise RuntimeError("transient store blip")
        return real_get(ids)

    monkeypatch.setattr(conversation_store, "get_conversations", flaky_get)
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        _recv_until(ws, {"snapshot"})
        # Mutate after the failing tick is guaranteed to occur (call 2);
        # a later successful tick must observe it. If the failed tick had
        # killed the ticker, the connection would close and _recv_until
        # would raise WebSocketDisconnect instead of returning a frame.
        conversation_store.update_conversation(s1, title="after")
        changed = _recv_until(ws, {"changed"})
        items = {item["id"]: item for item in changed["items"]}  # type: ignore[index]
        assert items[s1]["title"] == "after"
    # >= 3 proves at least one rescan ran after the injected failure
    # (call 1 = snapshot, call 2 = failed tick, call 3+ = recovered ticks).
    assert calls["n"] >= 3, (
        f"expected the ticker to keep rescanning after a failed tick, "
        f"but get_conversations was called only {calls['n']} time(s)"
    )


def test_session_added_event_pushes_unwatched_session(
    app: FastAPI, stores, fast_rescan: None
) -> None:
    """A ``session_added`` discovery event pushes a session the client isn't
    watching — the path that lets a session created elsewhere (another tab /
    CLI) enter the sidebar without a list poll.

    The client can never put an unknown session in its watch-set, so the
    interval diff can't surface it; the server reacts to the create/grant
    event instead. The pushed frame carries the real persisted row (fetched,
    not echoed from the event), which is what the client reconciles into the
    list. ``fast_rescan`` shrinks the heartbeat cadence so that if the push
    regressed, ``_recv_until`` exhausts its frame budget on heartbeats and
    fails fast instead of blocking on the 30 s production heartbeat.
    """
    s1 = _seed_session(stores, owner=ALICE, title="watched")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        _recv_until(ws, {"snapshot"})
        # A brand-new session is created for Alice elsewhere and announced on
        # her discovery channel — it is NOT in the watch-set above.
        s2 = _seed_session(stores, owner=ALICE, title="brand new")
        sessions_routes.user_session_stream.publish(
            ALICE, {"type": "session_added", "session_id": s2}
        )
        changed = _recv_until(ws, {"changed"})
        items = {item["id"]: item for item in changed["items"]}  # type: ignore[index]
        # The unwatched new session is pushed, carrying its persisted title —
        # proving the server fetched the real row, not echoed the event.
        assert s2 in items
        assert items[s2]["title"] == "brand new"


def test_session_added_for_inaccessible_session_is_not_pushed(
    app: FastAPI, stores, fast_rescan: None
) -> None:
    """A discovery announcement for a session the user can't access is dropped.

    The per-id access check in the push path is the cross-user safety net: even
    if a ``session_added`` for Bob's session landed on Alice's channel (e.g. a
    future mis-keyed publish), Alice must never be pushed a session she has no
    grant on. A regression that skipped the access check would leak Bob's row
    into Alice's stream as a ``changed`` frame.
    """
    s1 = _seed_session(stores, owner=ALICE, title="alice")
    bob_session = _seed_session(stores, owner=BOB, title="bob only")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        _recv_until(ws, {"snapshot"})
        # Announce Bob's session on Alice's channel; the access check must drop it.
        sessions_routes.user_session_stream.publish(
            ALICE, {"type": "session_added", "session_id": bob_session}
        )
        # Alice's watched session is static, so every frame must be a heartbeat;
        # a ``changed`` here (carrying bob_session) would be a cross-user leak.
        for _ in range(5):
            frame = json.loads(ws.receive_text())
            assert frame["type"] == "heartbeat", (
                f"expected only heartbeats, got {frame['type']} "
                f"(a changed frame would mean Bob's session leaked to Alice)"
            )


# ── comments fingerprint ──────────────────────────────────────────────


# One second in microseconds — comments_updated_at is epoch-µs.
_US = 1_000_000


@pytest.fixture
def comment_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Deterministic write clock for comment rows.

    Patches the ``now_epoch_us`` reference the comment store uses for
    all comment timestamps (``created_at`` derives from the same read)
    so consecutive comment writes get distinct, controlled values.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: Mutable ``{"now": <epoch seconds>}`` dict; assign
        ``clock["now"]`` to move time forward.
    """
    state = {"now": 1_000}
    monkeypatch.setattr(
        "omnigent.stores.comment_store.sqlalchemy_store.now_epoch_us",
        lambda: state["now"] * _US,
    )
    return state


def test_comment_add_pushes_changed_frame(
    app: FastAPI,
    stores,
    comment_store: SqlAlchemyCommentStore,
    comment_clock: dict[str, int],
    fast_rescan: None,
) -> None:
    """Adding a comment to a watched session pushes a ``changed`` frame.

    This is the freshness path the CommentsPanel relies on: another
    user's POST /comments must surface on this stream so the web app
    can invalidate its cached comment list without polling.
    """
    s1 = _seed_session(stores, owner=ALICE, title="commented")
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        snapshot = _recv_until(ws, {"snapshot"})
        items = {item["id"]: item for item in snapshot["items"]}
        # Baseline: no comments yet. The stream dumps full rows, so the
        # fingerprint fields must be present (count 0 / explicit null).
        assert items[s1]["comments_count"] == 0
        assert items[s1]["comments_updated_at"] is None

        comment_store.add(
            conversation_id=s1,
            path="src/app.py",
            body="needs a null check",
            start_index=0,
            end_index=10,
        )
        changed = _recv_until(ws, {"changed"})
        changed_items = {item["id"]: item for item in changed["items"]}
        # The add must move both fingerprint fields; a frame without them
        # means the WS builder isn't reading the comment store.
        assert changed_items[s1]["comments_count"] == 1
        assert changed_items[s1]["comments_updated_at"] == 1_000 * _US


def test_comment_status_change_pushes_changed_frame(
    app: FastAPI,
    stores,
    comment_store: SqlAlchemyCommentStore,
    comment_clock: dict[str, int],
    fast_rescan: None,
) -> None:
    """Marking a comment addressed pushes a ``changed`` frame.

    This is the agent flow: ``update_comment`` (status draft →
    addressed) changes no row count, so the ``updated_at`` bump is the
    only signal — if it didn't move, other viewers would keep showing
    the comment as open.
    """
    s1 = _seed_session(stores, owner=ALICE, title="addressed")
    comment = comment_store.add(
        conversation_id=s1,
        path="src/app.py",
        body="fix me",
        start_index=0,
        end_index=6,
    )
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        snapshot = _recv_until(ws, {"snapshot"})
        items = {item["id"]: item for item in snapshot["items"]}
        assert items[s1]["comments_count"] == 1
        assert items[s1]["comments_updated_at"] == 1_000 * _US

        comment_clock["now"] = 2_000
        comment_store.update_comment(comment.id, s1, status="addressed")
        changed = _recv_until(ws, {"changed"})
        changed_items = {item["id"]: item for item in changed["items"]}
        # Count is unchanged — the moved timestamp alone must trigger the
        # frame. A stale creation-time value here means update_comment didn't bump
        # updated_at and in-place edits are invisible to clients.
        assert changed_items[s1]["comments_count"] == 1
        assert changed_items[s1]["comments_updated_at"] == 2_000 * _US


def test_comment_delete_of_older_comment_pushes_changed_frame(
    app: FastAPI,
    stores,
    comment_store: SqlAlchemyCommentStore,
    comment_clock: dict[str, int],
    fast_rescan: None,
) -> None:
    """Deleting a non-newest comment pushes a ``changed`` frame.

    The deleted row is not the most recently updated one, so
    ``max(updated_at)`` is unchanged — the count drop is the only
    signal. This is the case that justifies ``comments_count``.
    """
    s1 = _seed_session(stores, owner=ALICE, title="deleted")
    older = comment_store.add(
        conversation_id=s1, path="a.py", body="old", start_index=0, end_index=3
    )
    comment_clock["now"] = 2_000
    comment_store.add(conversation_id=s1, path="a.py", body="new", start_index=4, end_index=7)
    with TestClient(app).websocket_connect(
        "/v1/sessions/updates", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        ws.send_text(json.dumps({"type": "watch", "session_ids": [s1]}))
        snapshot = _recv_until(ws, {"snapshot"})
        items = {item["id"]: item for item in snapshot["items"]}
        assert items[s1]["comments_count"] == 2
        assert items[s1]["comments_updated_at"] == 2_000 * _US

        comment_store.delete(older.id, s1)
        changed = _recv_until(ws, {"changed"})
        changed_items = {item["id"]: item for item in changed["items"]}
        # The timestamp must NOT move (the surviving comment is the
        # newest) — count 2 → 1 is what fires the frame. If this hangs
        # at _recv_until, deletes of older comments are invisible.
        assert changed_items[s1]["comments_count"] == 1
        assert changed_items[s1]["comments_updated_at"] == 2_000 * _US


def test_daily_cost_recorded_for_owned_session_without_a_policy(app: FastAPI, stores) -> None:
    """Per-user daily cost is recorded even when the session has no policy.

    The policy gate that previously suppressed the ``user_daily_cost`` write on
    no-policy sessions has been removed, so the daily rollup accrues for any
    owned session that records spend. Here ``ag_test`` has no guardrails/policy,
    yet posting cumulative spend must still land in the owner's daily rollup. A
    regression that re-added the gate would leave ``get_daily_cost`` at 0.0.
    """
    conversation_store = stores[0]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sid = _seed_session(stores, owner=ALICE, title="no-policy session")
    resp = TestClient(app).post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.5}},
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 202, resp.text
    # Recorded under the owner despite no policy on the session — proves the
    # write is no longer policy-gated.
    assert conversation_store.get_daily_cost(ALICE, today) == pytest.approx(0.5)


def test_daily_cost_tracks_display_cost_not_policy_cost(app: FastAPI, stores) -> None:
    """The daily rollup follows ``total_cost_usd`` (S), not ``policy_cost_usd``.

    claude-native posts the statusLine total ``S`` as ``cumulative_cost_usd``
    (display) and a higher ``max(S, C)`` as ``policy_cost_usd`` (the real-time
    gate value, inflated mid-turn by in-flight sub-agent spend). The per-user
    daily report must reflect real spend, so its delta comes from
    ``total_cost_usd`` = ``S``. A regression that fed ``policy_cost_usd`` into
    the rollup would over-report the daily total by the gate's mid-turn lead.
    """
    conversation_store = stores[0]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sid = _seed_session(stores, owner=ALICE, title="cost split session")
    resp = TestClient(app).post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "external_session_usage",
            # Display S = 0.20; enforcement value 0.90 leads it (sub-agent mid-run).
            "data": {"cumulative_cost_usd": 0.20, "policy_cost_usd": 0.90},
        },
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 202, resp.text
    # Daily = S (0.20), NOT the 0.90 enforcement figure. A value of 0.90 would
    # mean the rollup tracked policy_cost_usd and inherited the gate's
    # mid-turn inflation — the daily over-report this split prevents.
    assert conversation_store.get_daily_cost(ALICE, today) == pytest.approx(0.20)
