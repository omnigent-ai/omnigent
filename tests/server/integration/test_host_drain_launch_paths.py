"""Every session launch path refuses a draining host, through the full app.

Session create with ``host_id``, the message-dispatch relaunch, and the
retry/wake relaunch all funnel through launch admission; these tests drive
them end to end against ``create_app`` and assert that no launch side effect
reaches the host and the client gets a clear ``host_draining`` error.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.host.frames import (
    CAP_LAUNCH_DRAIN,
    HOST_DRAINING_ERROR_CODE,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostSetDrainFrame,
    HostStatFrame,
    HostStatResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.budgets import Deadline, budget
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "7a0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e"
_WORKSPACE = "/work/repo"


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path) -> FastAPI:  # type: ignore[no-untyped-def]
    """Full app with host routes mounted (overrides the conftest ``app``)."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )


def _scope(path: str) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


async def _connect_host(app: FastAPI) -> ApplicationCommunicator:
    comm = ApplicationCommunicator(app, _scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=budget(1.0))
    assert accepted["type"] == "websocket.accept"
    hello = HostHelloFrame(
        version="0.1.0-test",
        frame_protocol_version=1,
        name="laptop",
        capabilities=[CAP_LAUNCH_DRAIN],
    )
    await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(hello)})
    while app.state.host_registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    return comm


async def _serve_launch(
    comm: ApplicationCommunicator,
    *,
    status: str = "launched",
    error_code: str | None = None,
) -> HostLaunchRunnerFrame:
    """Answer the stat + launch round-trips of one host launch."""
    deadline = Deadline(30.0)
    for _ in range(40):
        output = await comm.receive_output(timeout=deadline.next_wait(30.0))
        if output["type"] != "websocket.send":
            continue
        frame = decode_host_frame(output["text"])
        if isinstance(frame, HostStatFrame):
            reply: Any = HostStatResultFrame(
                request_id=frame.request_id,
                status="ok",
                exists=True,
                type="directory",
                canonical_path=frame.path,
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            reply = HostLaunchRunnerResultFrame(
                request_id=frame.request_id,
                status=status,
                runner_id="runner_from_host" if status == "launched" else None,
                error=None if status == "launched" else "host is draining",
                error_code=error_code,
            )
        else:
            continue
        await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(reply)})
        if isinstance(frame, HostLaunchRunnerFrame):
            return frame
    raise AssertionError("host never received a launch frame")


async def _create_bound_session(
    client: httpx.AsyncClient, app: FastAPI, comm: ApplicationCommunicator
) -> dict[str, Any]:
    agent = await create_test_agent(client)
    responder = asyncio.create_task(_serve_launch(comm))
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
    )
    await responder
    assert resp.status_code == 201, resp.text
    return resp.json()


def _spy_launch_frames(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Mock:
    registry = app.state.host_registry
    send = Mock(wraps=registry.send_text)
    monkeypatch.setattr(registry, "send_text", send)
    return send


def _launch_frames(send: Mock) -> list[HostLaunchRunnerFrame]:
    frames = [decode_host_frame(call.args[1]) for call in send.call_args_list]
    return [frame for frame in frames if isinstance(frame, HostLaunchRunnerFrame)]


def _assert_host_draining(resp: httpx.Response) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == HOST_DRAINING_ERROR_CODE


async def _drain(client: httpx.AsyncClient) -> None:
    resp = await client.post(f"/v1/hosts/{_HOST_ID}/drain")
    assert resp.status_code == 200, resp.text
    assert resp.json()["draining"] is True


async def test_session_create_on_draining_host_is_rejected_before_any_row(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comm = await _connect_host(app)
    agent = await create_test_agent(client)
    await _drain(client)
    send = _spy_launch_frames(app, monkeypatch)
    before = len(SqlAlchemyConversationStore(db_uri).list_conversations(limit=100).data)

    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
    )

    _assert_host_draining(resp)
    assert _launch_frames(send) == []
    assert not [c for c in send.call_args_list if "host.stat" in c.args[1]]
    after = len(SqlAlchemyConversationStore(db_uri).list_conversations(limit=100).data)
    assert after == before, "a rejected create must not leave a session row behind"
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_message_relaunch_on_draining_host_is_rejected(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message to a session whose runner is gone must not relaunch while draining."""
    from omnigent.runtime import set_runner_client
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0.0)
    comm = await _connect_host(app)
    session = await _create_bound_session(client, app, comm)
    await _drain(client)
    send = _spy_launch_frames(app, monkeypatch)
    set_runner_client(None)
    try:
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
    finally:
        set_runner_client(None)

    _assert_host_draining(resp)
    assert _launch_frames(send) == []
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session["id"])
    assert conv is not None
    assert conv.runner_id == session["runner_id"], "binding must be untouched"
    items = await client.get(f"/v1/sessions/{session['id']}/items")
    assert items.json()["data"] == [], "the message is not consumed; the client retries"
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_relaunch_refused_by_host_after_admission_surfaces_host_draining(
    client: httpx.AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fence went up between admission and frame receipt: the host refuses."""
    from omnigent.runtime import set_runner_client
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0.0)
    comm = await _connect_host(app)
    session = await _create_bound_session(client, app, comm)
    set_runner_client(None)
    responder = asyncio.create_task(
        _serve_launch(comm, status="failed", error_code=HOST_DRAINING_ERROR_CODE)
    )
    try:
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
    finally:
        await responder
        set_runner_client(None)
    _assert_host_draining(resp)
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_drain_frame_precedes_any_launch_on_the_tunnel(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Through the full app: draining pushes the fence to the connected host."""
    comm = await _connect_host(app)
    await _drain(client)
    for _ in range(40):
        output = await comm.receive_output(timeout=budget(2.0))
        if output["type"] == "websocket.send":
            frame = decode_host_frame(output["text"])
            assert not isinstance(frame, HostLaunchRunnerFrame)
            if isinstance(frame, HostSetDrainFrame):
                assert frame == HostSetDrainFrame(generation=1, draining=True)
                break
    else:
        raise AssertionError("host never received host.set_drain")
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_managed_relaunch_under_draining_host_never_provisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new sandbox generation for a draining host identity is refused up front.

    Covers every relaunch caller (message wake, lost-sandbox recreate, and a
    continue-mode automation replacing its sandbox) at the shared pipeline.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.stores.host_store import Host

    provisioned: list[object] = []

    async def _provision(**kwargs: object) -> None:
        provisioned.append(kwargs)

    from omnigent.server.routes._sessions import orchestration as orchestration_module

    monkeypatch.setattr(sessions_module, "_provision_managed_sandbox", _provision)
    monkeypatch.setattr(orchestration_module, "_provision_managed_sandbox", _provision)
    failures: list[tuple[str, str]] = []
    tracker = Mock()
    tracker.fail.side_effect = lambda sid, reason: failures.append((sid, reason))
    host = Host(
        host_id=_HOST_ID,
        name="sandbox",
        user_id="alice",
        status="offline",
        created_at=1,
        updated_at=1,
        sandbox_provider="kubernetes",
        drain_generation=1,
        drain_started_at=1,
    )

    await sessions_module._run_managed_launch(
        session_id="conv_1",
        owner="alice",
        sandbox_config=Mock(),
        repos=(),
        tracker=tracker,
        conversation_store=Mock(),
        host_store=Mock(),
        host_registry=None,
        tunnel_registry=None,
        relaunch_host=host,
    )

    assert provisioned == []
    assert failures and failures[0][0] == "conv_1"
    assert "draining" in failures[0][1]
