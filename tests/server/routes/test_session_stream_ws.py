"""Tests for the ``WS /v1/sessions/{id}/stream/ws`` event stream.

The functional twin of the SSE ``GET /v1/sessions/{id}/stream`` route,
carried over a WebSocket so it rides the browser's separate connection
pool instead of the ~6-per-origin HTTP/1.1 pool the SSE stream competes
in. These tests drive the real route (no store/auth mocks) against
file-backed SQLite stores: they assert the subscription acknowledgment
heartbeat, live fan-out of a published event, the resource
snapshot-on-connect, and the unauthenticated-handshake reject.

The wire protocol is one JSON text frame per ``ServerStreamEvent``; a
normal close is the analog of the SSE ``[DONE]`` sentinel.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"


class _NoIdentityAuthProvider:
    """Auth provider whose handshake yields no identity (see the updates-ws
    test) — exercises the reject-when-unauthenticated gate deterministically."""

    def get_user_id(self, request: object) -> None:
        """Always return ``None`` (no authenticated identity)."""
        del request
        return


@pytest.fixture
def stores(
    db_uri: str,
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore]:
    """Real file-backed stores so writes from the test thread are visible to
    the WS handler thread."""
    return (
        SqlAlchemyConversationStore(db_uri),
        SqlAlchemyAgentStore(db_uri),
        SqlAlchemyPermissionStore(db_uri),
    )


@pytest.fixture
def app(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> FastAPI:
    """Minimal app mounting the sessions router with header auth and a real
    permission store — the surface the event WebSocket exercises."""
    conversation_store, agent_store, permission_store = stores
    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permission_store,
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
    """Create a session-shaped conversation owned by ``owner`` and return its id."""
    conversation_store, agent_store, permission_store = stores
    if agent_store.get("087b7cb7ac30abf4debfaa578d052ec6") is None:
        agent_store.create(
            agent_id="087b7cb7ac30abf4debfaa578d052ec6",
            name="test-agent",
            bundle_location="087b7cb7ac30abf4debfaa578d052ec6/bundle",
        )
    conv = conversation_store.create_conversation(
        title=title, agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    permission_store.ensure_user(owner)
    permission_store.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _recv_until(ws: object, wanted: set[str], *, max_frames: int = 50) -> dict[str, object]:
    """Read frames until one whose ``type`` is in ``wanted`` arrives, skipping
    heartbeats/snapshot events the test isn't awaiting."""
    for _ in range(max_frames):
        frame = json.loads(ws.receive_text())  # type: ignore[attr-defined]
        if frame.get("type") in wanted:
            return frame
    raise AssertionError(f"no frame in {wanted} after {max_frames} frames")


def test_stream_ws_acks_with_heartbeat(app: FastAPI, stores) -> None:
    """Connecting sends a ``session.heartbeat`` ack once the live-tail slot is
    registered — the WS analog of the SSE ready-event, so the client can wait
    for a concrete subscription before posting a fast one-shot turn."""
    sid = _seed_session(stores, owner=ALICE, title="s")
    with TestClient(app).websocket_connect(
        f"/v1/sessions/{sid}/stream/ws", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "session.heartbeat"


def test_stream_ws_fans_out_published_event(app: FastAPI, stores) -> None:
    """An event published onto the session after connect is delivered live as
    one JSON frame — the core fan-out the SSE route also relies on."""
    sid = _seed_session(stores, owner=ALICE, title="s")
    with TestClient(app).websocket_connect(
        f"/v1/sessions/{sid}/stream/ws", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        # Wait for the subscription ack so the subscriber slot is registered
        # before we publish (the broker has no buffer / no replay).
        assert json.loads(ws.receive_text())["type"] == "session.heartbeat"
        session_stream.publish(
            sid,
            {"type": "response.output_text.delta", "delta": "hello", "item_id": "m1"},
        )
        evt = _recv_until(ws, {"response.output_text.delta"})
        assert evt["delta"] == "hello"


def test_stream_ws_snapshot_includes_presence(app: FastAPI, stores) -> None:
    """The snapshot-on-connect carries the same resource events as the SSE
    route — at minimum the changed-files invalidate and a presence frame — so
    a fresh WS client hydrates without a separate poll."""
    sid = _seed_session(stores, owner=ALICE, title="s")
    with TestClient(app).websocket_connect(
        f"/v1/sessions/{sid}/stream/ws", headers={"X-Forwarded-Email": ALICE}
    ) as ws:
        invalidate = _recv_until(ws, {"session.changed_files.invalidated"})
        assert invalidate["session_id"] == sid
        presence = _recv_until(ws, {"session.presence"})
        # The connecting viewer registered itself, so the snapshot presence
        # frame is scoped to this session and non-empty.
        assert presence["conversation_id"] == sid


def test_stream_ws_rejects_unauthenticated(stores) -> None:
    """With permissions enabled, a handshake with no identity is closed at the
    handshake (1008 policy violation) before any session data is read."""
    conversation_store, agent_store, permission_store = stores
    sid = _seed_session(stores, owner=ALICE, title="s")
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
        with TestClient(app).websocket_connect(f"/v1/sessions/{sid}/stream/ws"):
            pass
    assert exc_info.value.code == 1008


def test_stream_ws_rejects_unauthorized_user(app: FastAPI, stores) -> None:
    """A user without access to the session is denied — the id is never
    trusted from the client for authorization."""
    sid = _seed_session(stores, owner=ALICE, title="s")
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(
            f"/v1/sessions/{sid}/stream/ws", headers={"X-Forwarded-Email": BOB}
        ):
            pass
