"""Backend permission denials are handled consistently across session APIs."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.errors import ErrorCategory
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_session

_DENIAL_DETAILS = "Received http2 header with status: 403"


@pytest.fixture
async def catchall_client(
    app: FastAPI, client: httpx.AsyncClient
) -> AsyncIterator[httpx.AsyncClient]:
    # Starlette re-raises after sending a catch-all response; preserve that response.
    # The shared client fixture handles the app's setup and background-task cleanup.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _rpc_error(status_name: str, implementation: str = "vendored") -> Exception:
    if implementation == "grpcio":
        grpc = pytest.importorskip("grpc")
        base = grpc.RpcError
        status = getattr(grpc.StatusCode, status_name)
    else:
        # A separately defined RpcError must match even without grpcio installed.
        base = type("RpcError", (Exception,), {})
        status = SimpleNamespace(name=status_name)

    class BackendRpcError(base):
        def code(self) -> object:
            return status

        def details(self) -> str:
            return _DENIAL_DETAILS

    return BackendRpcError(_DENIAL_DETAILS)


@pytest.mark.parametrize("implementation", ["vendored", "grpcio"])
@pytest.mark.parametrize(
    ("method", "suffix", "store_method"),
    [
        pytest.param("GET", "", "list_conversations", id="listing"),
        pytest.param("GET", "/items", "_decode_item_data_batch", id="decrypt"),
        pytest.param("POST", "/events", "_encode_item_data_batch", id="encrypt"),
    ],
)
async def test_permission_denied_maps_to_403(
    catchall_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    implementation: str,
    method: str,
    suffix: str,
    store_method: str,
) -> None:
    session = await create_test_session(catchall_client, name="denial-test")
    store = SqlAlchemyConversationStore(db_uri)
    store.append(
        session["id"],
        [
            NewConversationItem(
                type="message",
                response_id="resp_seed",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hello"}]),
            )
        ],
    )
    path = f"/v1/sessions/{session['id']}{suffix}" if suffix else "/v1/sessions"
    # Replace only the external backend boundary, leaving routes and persistence real.
    denied = Mock(side_effect=_rpc_error("PERMISSION_DENIED", implementation))
    monkeypatch.setattr(SqlAlchemyConversationStore, store_method, denied)
    kwargs = {}
    if method == "POST":
        kwargs["json"] = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "denied"}],
                },
                "response_id": "resp_denied",
            },
        }
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="omnigent.server.app"):
        response = await catchall_client.request(method, path, **kwargs)

    denied.assert_called()
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "upstream_permission_denied"
    assert path in error["message"]
    assert "ask an administrator" in error["message"]
    (record,) = [r for r in caplog.records if r.name == "omnigent.server.app"]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("Upstream call denied by a backing service:")
    assert record.exc_info is not None
    assert record.attributes["code"] == error["code"]
    assert record.attributes["http_status"] == "403"
    assert record.attributes["error_category"] == ErrorCategory.UPSTREAM.value


@pytest.mark.parametrize("status_name", ["UNAVAILABLE", "UNAUTHENTICATED"])
async def test_other_rpc_errors_remain_500(
    catchall_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_name: str,
) -> None:
    monkeypatch.setattr(
        SqlAlchemyConversationStore,
        "list_conversations",
        Mock(side_effect=_rpc_error(status_name)),
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.server.app"):
        response = await catchall_client.get("/v1/sessions")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    (record,) = [r for r in caplog.records if r.name == "omnigent.server.app"]
    assert record.levelno == logging.ERROR
    assert record.getMessage().startswith("Unhandled exception:")
    assert record.exc_info is not None
    assert record.attributes["http_status"] == "500"
