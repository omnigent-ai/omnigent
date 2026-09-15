"""Cross-replica forwarding of mis-routed session requests.

A session's runner tunnel is replica-local: a request that lands on a
replica without the tunnel used to answer ``400 wrong_replica`` and rely
on the client's single keyless re-address, which only reaches the ingress
default replica — any session whose tunnel lived elsewhere stranded.

These tests drive the events and stream routes on a replica that does not
hold the session's host tunnel and verify it proxies the request to the
owning replica's advertised URL (from ``hosts.replica_url``) and relays
the response — and that it falls back to the pre-existing
``wrong_replica`` error whenever forwarding is not possible (the request
already crossed one hop, no URL is advertised, the row points back at
this replica, or the owning replica is unreachable).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import replica_forward
from omnigent.server.app import create_app
from omnigent.server.replica_forward import REPLICA_FORWARDED_HEADER
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_OWN_ADVERTISE_URL = "http://replica-a:8000"
_PEER_ADVERTISE_URL = "http://replica-b:8000"
_HOST_ID = "9c8b7a6f5e4d3c2b1a09182736455463"

_MESSAGE_EVENT: dict[str, Any] = {
    "type": "message",
    "data": {
        "role": "user",
        "content": [{"type": "input_text", "text": "follow-up after the mis-route"}],
    },
}


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """The replica under test: host-aware, no tunnels, an advertised URL.

    :param runtime_init: Initializes runtime globals (shared fixture).
    :param db_uri: Per-test database URI (shared fixture).
    :param tmp_path: Pytest temp directory for artifacts and cache.
    :returns: A FastAPI app standing in for the mis-routed replica.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        replica_advertise_url=_OWN_ADVERTISE_URL,
    )


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process client against the mis-routed replica.

    :param app: The replica under test.
    :yields: An ASGI-transport client.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _misrouted_session(client: httpx.AsyncClient, db_uri: str) -> str:
    """Create a session whose host is live on another replica.

    The host row is online with a fresh heartbeat (live "somewhere") but
    the app's replica-local host registry has no tunnel for it — the
    wrong-replica landing this suite exercises.

    :param client: Client for the replica under test.
    :param db_uri: The shared database URI.
    :returns: The session id.
    """
    agent = await create_test_agent(client, name="replica-forward-agent")
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    session_id = str(resp.json()["id"])
    SqlAlchemyConversationStore(db_uri).set_host_id(
        session_id, host_id=_HOST_ID, workspace="/tmp/ws"
    )
    return session_id


def _stamp_host(db_uri: str, replica_url: str | None) -> None:
    """Register the host row as live, tunneled at *replica_url*.

    :param db_uri: The shared database URI.
    :param replica_url: The owning replica's advertised URL, or None.
    """
    HostStore(db_uri).upsert_on_connect(
        _HOST_ID, "remote-host", "owner@example.com", replica_url=replica_url
    )


def _install_forward_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    """Route the forwarding client through an in-process mock transport.

    :param monkeypatch: Pytest patcher.
    :param handler: ``httpx.MockTransport`` handler for the peer replica.
    :returns: The list the handler's requests are recorded into.
    """
    seen: list[httpx.Request] = []

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_recording_handler), timeout=timeout
        )

    monkeypatch.setattr(replica_forward, "_build_async_client", _factory)
    return seen


async def test_misrouted_event_post_forwards_to_owning_replica(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send that lands on the wrong replica is proxied, not stranded.

    The forwarded hop must target the owning replica's advertised URL,
    preserve the request body, and carry the forwarded-guard header; the
    peer's acknowledgement is relayed verbatim to the caller.
    """
    session_id = await _misrouted_session(client, db_uri)
    _stamp_host(db_uri, _PEER_ADVERTISE_URL)
    seen = _install_forward_transport(
        monkeypatch,
        lambda _req: httpx.Response(202, json={"queued": True, "item_id": "fwd-1"}),
    )

    resp = await client.post(f"/v1/sessions/{session_id}/events", json=_MESSAGE_EVENT)

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": True, "item_id": "fwd-1"}
    assert len(seen) == 1
    forwarded = seen[0]
    assert str(forwarded.url) == f"{_PEER_ADVERTISE_URL}/v1/sessions/{session_id}/events"
    assert forwarded.headers[REPLICA_FORWARDED_HEADER] == "1"
    assert json.loads(forwarded.content) == _MESSAGE_EVENT


async def test_already_forwarded_request_is_not_forwarded_again(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that crossed one hop fails as before — no forwarding loop.

    A stale ``replica_url`` chain must terminate at the second replica
    with the ordinary wrong-replica error, not bounce between replicas.
    """
    session_id = await _misrouted_session(client, db_uri)
    _stamp_host(db_uri, _PEER_ADVERTISE_URL)
    seen = _install_forward_transport(
        monkeypatch, lambda _req: httpx.Response(202, json={"queued": True})
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_MESSAGE_EVENT,
        headers={REPLICA_FORWARDED_HEADER: "1"},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "wrong_replica"
    assert seen == []


@pytest.mark.parametrize(
    "row_replica_url",
    [None, _OWN_ADVERTISE_URL],
    ids=["no_advertised_url", "row_points_at_this_replica"],
)
async def test_unforwardable_misroute_falls_back_to_wrong_replica(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    row_replica_url: str | None,
) -> None:
    """Without a foreign advertised URL the pre-existing error surfaces.

    Covers a row written by a replica with no advertised address and a
    stale row pointing back at the receiving replica itself.
    """
    session_id = await _misrouted_session(client, db_uri)
    _stamp_host(db_uri, row_replica_url)
    seen = _install_forward_transport(
        monkeypatch, lambda _req: httpx.Response(202, json={"queued": True})
    )

    resp = await client.post(f"/v1/sessions/{session_id}/events", json=_MESSAGE_EVENT)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "wrong_replica"
    assert seen == []


async def test_unreachable_owning_replica_falls_back_to_wrong_replica(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead advertised URL degrades to the pre-existing error.

    The owning replica can die between stamping the row and the forward;
    the caller must get the re-addressable 400, not a 5xx surprise.
    """
    session_id = await _misrouted_session(client, db_uri)
    _stamp_host(db_uri, _PEER_ADVERTISE_URL)

    def _refuse(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_forward_transport(monkeypatch, _refuse)

    resp = await client.post(f"/v1/sessions/{session_id}/events", json=_MESSAGE_EVENT)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "wrong_replica"


async def test_misrouted_stream_is_proxied_from_owning_replica(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live SSE tail follows the runner to the owning replica.

    Healing the send alone is not enough — the reply streams over this
    endpoint, so a mis-routed stream connect must relay the owner's SSE
    bytes (status, content type, and body) instead of erroring.
    """
    session_id = await _misrouted_session(client, db_uri)
    SqlAlchemyConversationStore(db_uri).set_runner_id(session_id, "runner-elsewhere")
    _stamp_host(db_uri, _PEER_ADVERTISE_URL)
    sse_payload = b'data: {"type": "session.status", "status": "running"}\n\n'

    class _SSEBody(httpx.AsyncByteStream):
        """Unconsumed async body, as a live upstream SSE response carries."""

        async def __aiter__(self) -> Any:
            yield sse_payload

    seen = _install_forward_transport(
        monkeypatch,
        lambda _req: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_SSEBody()
        ),
    )

    resp = await client.get(f"/v1/sessions/{session_id}/stream")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == sse_payload
    assert len(seen) == 1
    forwarded = seen[0]
    assert str(forwarded.url) == f"{_PEER_ADVERTISE_URL}/v1/sessions/{session_id}/stream"
    assert forwarded.headers[REPLICA_FORWARDED_HEADER] == "1"
