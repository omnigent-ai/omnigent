"""Tests for the server's Chrome browser attach relay route."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.runner.chrome_browser import WS_CLOSE_CHROME_BROWSER_NOT_CONFIGURED
from omnigent.runtime import _globals, set_runner_ws_factory
from omnigent.server._runner_ws_tunnel import make_tunnel_ws_factory
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes.chrome_browser_attach import create_chrome_browser_attach_router
from tests.server.routes.test_terminal_attach import (
    _FakeRunnerWSConn,
    _FakeRunnerWSFactory,
    _StubConversationStore,
    _StubPermissionStore,
)

_ATTACH_PATH = "/v1/sessions/conv_alice/resources/chrome_browser/attach"
_OWNER = {"X-Forwarded-Email": "owner@example.com"}
_READER = {"X-Forwarded-Email": "reader@example.com"}


@pytest.fixture(autouse=True)
def restore_runner_ws_factory() -> Iterator[None]:
    prior_factory = _globals._runner_ws_factory
    yield
    set_runner_ws_factory(prior_factory)


def _app_with_permissions() -> FastAPI:
    conversation_store = _StubConversationStore()
    conversation_store.add("conv_alice")
    permission_store = _StubPermissionStore()
    permission_store.add_grant("owner@example.com", "conv_alice", LEVEL_OWNER)
    permission_store.add_grant("reader@example.com", "conv_alice", LEVEL_READ)
    app = FastAPI()
    app.include_router(
        create_chrome_browser_attach_router(
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permission_store,  # type: ignore[arg-type]
            conversation_store=conversation_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return app


def test_owner_attach_relays_frames_both_ways() -> None:
    conn = _FakeRunnerWSConn(outgoing=[b"frame", '{"type":"ready"}'], wait_close_after=1)
    factory = _FakeRunnerWSFactory(conn)
    set_runner_ws_factory(factory)

    with TestClient(_app_with_permissions()).websocket_connect(_ATTACH_PATH, headers=_OWNER) as ws:
        assert ws.receive_bytes() == b"frame"
        assert ws.receive_text() == '{"type":"ready"}'
        ws.send_text('{"type":"reload"}')
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()

    assert factory.calls == [f"{_ATTACH_PATH}?read_only=false"]
    assert conn.received == ['{"type":"reload"}']


def test_reader_may_watch_but_not_drive() -> None:
    factory = _FakeRunnerWSFactory(_FakeRunnerWSConn(outgoing=[b"frame"]))
    set_runner_ws_factory(factory)
    client = TestClient(_app_with_permissions())

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(_ATTACH_PATH, headers=_READER):
            pass
    assert rejected.value.code == 1008
    assert factory.calls == []

    with client.websocket_connect(f"{_ATTACH_PATH}?read_only=true", headers=_READER) as ws:
        assert ws.receive_bytes() == b"frame"
    assert factory.calls == [f"{_ATTACH_PATH}?read_only=true"]


def test_runner_close_code_reaches_the_viewer() -> None:
    conn = _FakeRunnerWSConn(
        close_code=WS_CLOSE_CHROME_BROWSER_NOT_CONFIGURED, close_reason="not configured"
    )
    set_runner_ws_factory(_FakeRunnerWSFactory(conn))

    with TestClient(_app_with_permissions()).websocket_connect(_ATTACH_PATH, headers=_OWNER) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_bytes()

    assert closed.value.code == WS_CLOSE_CHROME_BROWSER_NOT_CONFIGURED
    assert closed.value.reason == "not configured"


def test_tunnel_factory_accepts_the_chrome_browser_path() -> None:
    class _NoRunnerRouter:
        def client_for_existing_conversation(self, conversation_id: str) -> None:
            self.conversation_id = conversation_id

    router = _NoRunnerRouter()
    factory = make_tunnel_ws_factory(router, registry=None)  # type: ignore[arg-type]

    # Reaching the router proves the path was recognized and its session id parsed.
    with pytest.raises(RuntimeError, match="no runner pinned"):
        factory(f"{_ATTACH_PATH}?read_only=false")
    assert router.conversation_id == "conv_alice"
