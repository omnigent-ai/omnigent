"""Host and session ownership, leased context RPCs, and terminal WS transport."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostWorkspaceContextRequestFrame,
    HostWorkspaceContextStreamFrame,
    decode_host_frame,
)
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_workspace_contexts import create_host_workspace_contexts_router

_BASE = "/v1/hosts/host_test/workspace-contexts"
_CONTEXT = f"{_BASE}/context_test"
_TERMINALS = f"{_CONTEXT}/resources/terminals"
_ATTACH = f"{_TERMINALS}/terminal_test/attach"


@pytest.fixture()
def setup() -> SimpleNamespace:
    """An in-memory host that replies through the real RPC future map."""
    registry = HostRegistry()
    conn = registry.register(
        host_id="host_test",
        ws=Mock(),
        hello=HostHelloFrame(
            version="test", frame_protocol_version=1, name="test", workspace_contexts=True
        ),
        owner="alice",
    )
    state = SimpleNamespace(
        registry=registry,
        conn=conn,
        sent=[],
        context={
            "id": "context_test",
            "workspace": "/repo",
            "session_id": None,
            "lease_seconds": 600,
        },
        error=None,
        stream_close=False,
    )
    conv = Conversation(
        id="session_test",
        root_conversation_id="session_test",
        created_at=0,
        updated_at=0,
        host_id="host_test",
        workspace="/repo",
    )
    conversations = Mock()
    conversations.get_conversation.side_effect = lambda sid: conv if sid == conv.id else None
    permissions = Mock()
    permissions.is_admin.return_value = False
    state.level = LEVEL_OWNER
    permissions.check_access.side_effect = lambda user, sid, level: (
        user == "alice" and sid == "session_test" and state.level >= level
    )
    hosts = Mock()
    hosts.get_host.side_effect = lambda hid: (
        SimpleNamespace(host_id=hid, user_id="alice") if hid == "host_test" else None
    )

    def send_text(connection: Any, data: str) -> None:
        frame = decode_host_frame(data)
        state.sent.append(frame)
        if isinstance(frame, HostWorkspaceContextStreamFrame):
            if frame.close_code is None:
                connection.workspace_context_streams[frame.channel_id].put_nowait(frame)
            return
        assert isinstance(frame, HostWorkspaceContextRequestFrame)
        future = connection.pending_workspace_contexts[frame.request_id]
        if state.error:
            future.set_result(state.error)
            return
        if frame.op == "handoff":
            if frame.params["workspace"] != state.context["workspace"]:
                future.set_result(
                    {"status": "failed", "error_status": 409, "error": "workspace mismatch"}
                )
                return
            state.context["session_id"] = frame.params["session_id"]
        if frame.op == "attach":
            queue = connection.workspace_context_streams[frame.params["channel_id"]]
            queue.put_nowait(
                HostWorkspaceContextStreamFrame(
                    channel_id=frame.params["channel_id"],
                    data=base64.b64encode(b"ready\xff").decode(),
                    binary=True,
                )
            )
            if state.stream_close:
                queue.put_nowait(
                    HostWorkspaceContextStreamFrame(
                        channel_id=frame.params["channel_id"],
                        close_code=1000,
                    )
                )
        if frame.op == "list_terminals":
            payload = {"object": "list", "data": [{"id": "terminal_test"}], "has_more": False}
        elif frame.op == "create_terminal":
            payload = {"id": "terminal_test", "session_key": frame.params["session_key"]}
        else:
            payload = dict(state.context)
        future.set_result({"status": "ok", "payload": payload})

    registry.send_text = send_text
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def error_handler(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.http_status)

    state.auth = UnifiedAuthProvider(source="header", local_single_user=False)
    app.include_router(
        create_host_workspace_contexts_router(
            registry,
            hosts,
            conversations,
            auth_provider=state.auth,
            permission_store=permissions,
        ),
        prefix="/v1",
    )
    state.client = TestClient(app, headers={"X-Forwarded-Email": "alice"})
    state.conv = conv
    return state


@pytest.mark.parametrize("user,expected", [(None, 401), ("bob", 403)])
def test_create_requires_host_owner(
    setup: SimpleNamespace, user: str | None, expected: int
) -> None:
    setup.client.headers.clear()
    if user:
        setup.client.headers["X-Forwarded-Email"] = user
    response = setup.client.post(_BASE, json={"workspace": "/repo"})
    assert response.status_code == expected
    assert setup.sent == []


def test_create_and_terminal_lifecycle(setup: SimpleNamespace) -> None:
    assert setup.client.post(_BASE, json={"workspace": "/repo"}).json() == setup.context
    create = setup.sent[-1]
    assert (create.op, create.user_id, create.params) == (
        "create",
        "alice",
        {"workspace": "/repo"},
    )
    assert setup.client.post(f"{_CONTEXT}/heartbeat").status_code == 200
    assert setup.client.get(_TERMINALS).json()["data"] == [{"id": "terminal_test"}]
    response = setup.client.post(_TERMINALS, json={"terminal": "bash", "session_key": "shell-2"})
    assert response.json()["session_key"] == "shell-2"
    assert setup.client.delete(f"{_TERMINALS}/terminal_test").status_code == 200
    assert setup.client.delete(_CONTEXT).status_code == 200
    assert not setup.conn.pending_workspace_contexts


@pytest.mark.parametrize("suffix", ["", "/resources/terminals", "/handoff"])
def test_mutating_body_requires_json(setup: SimpleNamespace, suffix: str) -> None:
    url = _BASE if not suffix else _CONTEXT + suffix
    assert (
        setup.client.post(url, content="{}", headers={"Content-Type": "text/plain"}).status_code
        == 415
    )
    assert not setup.sent


def test_draft_terminal_rejects_agent_harness(setup: SimpleNamespace) -> None:
    assert setup.client.post(_TERMINALS, json={"terminal": "codex"}).status_code == 422
    assert not setup.sent


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_handoff_requires_session_owner(setup: SimpleNamespace, level: int) -> None:
    setup.level = level
    response = setup.client.post(f"{_CONTEXT}/handoff", json={"session_id": "session_test"})
    assert response.status_code == (404 if level == 0 else 403)
    assert not setup.sent


def test_handoff_checks_session_host_before_contacting_host(setup: SimpleNamespace) -> None:
    setup.conv.host_id = "another_host"
    assert (
        setup.client.post(f"{_CONTEXT}/handoff", json={"session_id": "session_test"}).status_code
        == 409
    )
    assert not setup.sent


def test_handoff_checks_canonical_workspace_on_host(setup: SimpleNamespace) -> None:
    setup.conv.workspace = "/different"
    response = setup.client.post(f"{_CONTEXT}/handoff", json={"session_id": "session_test"})
    assert response.status_code == 409
    assert setup.sent[-1].params == {"session_id": "session_test", "workspace": "/different"}
    assert setup.context["session_id"] is None


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("post", "/heartbeat", None),
        ("delete", "", None),
        ("get", "/resources/terminals", None),
        ("post", "/resources/terminals", {"terminal": "bash"}),
        ("delete", "/resources/terminals/terminal_test", None),
    ],
)
def test_adopted_context_checks_session_owner_on_every_access(
    setup: SimpleNamespace,
    method: str,
    suffix: str,
    body: dict | None,
) -> None:
    assert (
        setup.client.post(f"{_CONTEXT}/handoff", json={"session_id": "session_test"}).status_code
        == 200
    )
    setup.level = 2
    setup.sent.clear()
    response = setup.client.request(method, _CONTEXT + suffix, json=body)
    assert response.status_code == 403
    assert [frame.op for frame in setup.sent] == ["describe"]


@pytest.mark.parametrize("status", [403, 404, 409, 429, 500])
def test_host_error_status_and_pending_cleanup(setup: SimpleNamespace, status: int) -> None:
    setup.error = {"status": "failed", "error_status": status, "error": "host refused"}
    response = setup.client.post(_BASE, json={"workspace": "/repo"})
    assert response.status_code == status
    assert response.json()["detail"] == "host refused"
    assert not setup.conn.pending_workspace_contexts


def test_rpc_disconnect_cleanup(setup: SimpleNamespace) -> None:
    setup.registry.send_text = Mock(side_effect=ConnectionError("gone"))
    assert setup.client.post(_BASE, json={"workspace": "/repo"}).status_code == 502
    assert not setup.conn.pending_workspace_contexts


def test_rpc_timeout_cleanup(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.server.routes.host_workspace_contexts._CONTEXT_TIMEOUT_S", 0.001)
    setup.registry.send_text = Mock()
    assert setup.client.post(_BASE, json={"workspace": "/repo"}).status_code == 504
    assert not setup.conn.pending_workspace_contexts


def test_websocket_binary_resize_and_cleanup(setup: SimpleNamespace) -> None:
    with setup.client.websocket_connect(_ATTACH) as ws:
        assert ws.receive_bytes() == b"ready\xff"
        ws.send_bytes(b"printf test\n")
        assert ws.receive_bytes() == b"printf test\n"
        ws.send_text('{"type":"resize","cols":100,"rows":40}')
        assert ws.receive_text() == '{"type":"resize","cols":100,"rows":40}'
    assert not setup.conn.workspace_context_streams
    assert setup.sent[-1].close_code == 1000


def test_websocket_read_only_drops_binary_input(setup: SimpleNamespace) -> None:
    with setup.client.websocket_connect(_ATTACH + "?read_only=true") as ws:
        assert ws.receive_bytes() == b"ready\xff"
        ws.send_bytes(b"forbidden")
        ws.send_text('{"type":"resize","cols":80,"rows":24}')
        assert ws.receive_text().startswith('{"type":"resize"')
    frames = [frame for frame in setup.sent if isinstance(frame, HostWorkspaceContextStreamFrame)]
    assert all(not frame.binary for frame in frames)


@pytest.mark.parametrize("user,adopted", [("bob", False), ("alice", True)])
def test_websocket_rejects_unauthorized_before_accept(
    setup: SimpleNamespace,
    user: str,
    adopted: bool,
) -> None:
    setup.client.headers["X-Forwarded-Email"] = user
    if adopted:
        setup.context["session_id"] = "session_test"
        setup.level = 1
    with pytest.raises(WebSocketDisconnect) as exc:
        with setup.client.websocket_connect(_ATTACH):
            pytest.fail("unauthorized websocket accepted")
    assert exc.value.code == 1008
    assert not setup.conn.workspace_context_streams
    assert all(getattr(frame, "op", None) == "describe" for frame in setup.sent)


def test_websocket_host_close_is_forwarded(setup: SimpleNamespace) -> None:
    setup.stream_close = True
    with setup.client.websocket_connect(_ATTACH) as ws:
        assert ws.receive_bytes() == b"ready\xff"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
        assert exc.value.code == 1000
    assert not setup.conn.workspace_context_streams


@pytest.mark.parametrize(
    "method,suffix", [("post", ""), ("get", "/context_test/resources/terminals")]
)
def test_old_host_capability_fails_without_rpc(
    setup: SimpleNamespace, method: str, suffix: str
) -> None:
    setup.conn.hello.workspace_contexts = False
    response = setup.client.request(method, _BASE + suffix, json={"workspace": "/repo"})
    assert response.status_code == 409
    assert "upgrade" in response.json()["detail"]
    assert not setup.sent


def test_websocket_authenticates_signed_browser_cookie(setup: SimpleNamespace) -> None:
    from omnigent.server.oidc import mint_session_token

    setup.auth._source = "accounts"
    setup.auth._accounts_config = SimpleNamespace(
        cookie_secret=b"test-cookie-signing-key-at-least-32-bytes", session_cookie_name="session"
    )
    setup.client.headers.clear()
    token = mint_session_token("alice", setup.auth._accounts_config.cookie_secret, 60, "accounts")
    setup.client.cookies.set("session", token)
    with setup.client.websocket_connect(_ATTACH) as ws:
        assert ws.receive_bytes() == b"ready\xff"
    attach = next(frame for frame in setup.sent if getattr(frame, "op", None) == "attach")
    assert attach.user_id == "alice"


def test_query_token_cannot_bypass_browser_identity(setup: SimpleNamespace) -> None:
    setup.client.headers.clear()
    with pytest.raises(WebSocketDisconnect) as exc:
        with setup.client.websocket_connect(_ATTACH + "?token=alice&user_id=alice"):
            pytest.fail("query string cannot authenticate a host-owned terminal")
    assert exc.value.code == 1008
    assert not setup.sent


@pytest.mark.parametrize("status,close_code", [(404, 4404), (403, 1008), (500, 1011)])
def test_websocket_host_errors_preserve_close_codes(
    setup: SimpleNamespace, status: int, close_code: int
) -> None:
    setup.error = {"status": "failed", "error_status": status, "error": "host refused"}
    with pytest.raises(WebSocketDisconnect) as exc:
        with setup.client.websocket_connect(_ATTACH) as ws:
            ws.receive_bytes()
    assert exc.value.code == close_code
    assert not setup.conn.pending_workspace_contexts
    assert not setup.conn.workspace_context_streams


@pytest.mark.parametrize("session_key", ["a" * 81, "bad/key", "with space", ""])
def test_terminal_session_key_validated_before_host_rpc(
    setup: SimpleNamespace, session_key: str
) -> None:
    response = setup.client.post(_TERMINALS, json={"session_key": session_key})
    assert response.status_code == 422
    assert not setup.sent


@pytest.mark.parametrize("binary", [True, False])
def test_websocket_oversized_input_rejected_before_host_queue(
    setup: SimpleNamespace, binary: bool
) -> None:
    with setup.client.websocket_connect(_ATTACH) as ws:
        assert ws.receive_bytes() == b"ready\xff"
        if binary:
            ws.send_bytes(b"a" * (192 * 1024 + 1))
        else:
            ws.send_text("a" * (256 * 1024 + 1))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
        assert exc.value.code == 1009
    stream_frames = [f for f in setup.sent if isinstance(f, HostWorkspaceContextStreamFrame)]
    assert all(f.close_code is not None for f in stream_frames)
    assert not setup.conn.workspace_context_streams


def test_websocket_backlog_does_not_grow_host_queue(setup: SimpleNamespace) -> None:
    with setup.client.websocket_connect(_ATTACH) as ws:
        assert ws.receive_bytes() == b"ready\xff"
        for _ in range(256):
            setup.conn.outbound_queue.put_nowait("queued-frame")
        ws.send_bytes(b"input")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
        assert exc.value.code == 1013
    stream_frames = [f for f in setup.sent if isinstance(f, HostWorkspaceContextStreamFrame)]
    assert all(f.close_code is not None for f in stream_frames)
    assert not setup.conn.workspace_context_streams


def test_missing_terminal_close_follows_successful_handshake(setup: SimpleNamespace) -> None:
    setup.error = {"status": "failed", "error_status": 404, "error": "missing"}
    with setup.client.websocket_connect(_ATTACH) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
        assert exc.value.code == 4404
