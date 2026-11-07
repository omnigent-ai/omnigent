"""Backend gRPC failures are mapped consistently across session APIs."""

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


def _user_item(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp_seed",
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


@pytest.fixture
async def catchall_client(
    app: FastAPI, client: httpx.AsyncClient
) -> AsyncIterator[httpx.AsyncClient]:
    # Starlette re-raises after sending a catch-all response; preserve that response.
    # The shared client fixture handles the app's setup and background-task cleanup.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _rpc_error(
    status_name: str, implementation: str = "vendored", detail: str = _DENIAL_DETAILS
) -> Exception:
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
            return detail

    return BackendRpcError(detail)


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
@pytest.mark.parametrize(
    ("suffix", "store_method"),
    [
        pytest.param("", "list_conversations", id="listing"),
        pytest.param("/items", "_decode_item_data_batch", id="decrypt"),
    ],
)
async def test_other_rpc_errors_remain_500(
    catchall_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_name: str,
    suffix: str,
    store_method: str,
) -> None:
    """Statuses outside the mapped set keep the unhandled-500 contract, on the
    listing and on the items read: an outage of the decode backend must not be
    served as a page of unreadable-item placeholders."""
    path = "/v1/sessions"
    if suffix:
        session = await create_test_session(catchall_client, name="decode-outage")
        SqlAlchemyConversationStore(db_uri).append(session["id"], [_user_item("hello")])
        path = f"/v1/sessions/{session['id']}{suffix}"
    monkeypatch.setattr(
        SqlAlchemyConversationStore, store_method, Mock(side_effect=_rpc_error(status_name))
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="omnigent.server.app"):
        response = await catchall_client.get(path)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    (record,) = [
        r
        for r in caplog.records
        if r.name == "omnigent.server.app" and r.levelno >= logging.WARNING
    ]
    assert record.levelno == logging.ERROR
    assert record.getMessage().startswith("Unhandled exception:")
    assert record.exc_info is not None
    assert record.attributes["http_status"] == "500"


async def test_payload_rejected_decrypt_degrades_only_that_item(
    catchall_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A decrypt backend refusing one oversized item (RESOURCE_EXHAUSTED) no
    longer fails the items page: the readable items load and the refused one is
    served as an ``item_data_unreadable`` placeholder, with no unhandled 500."""
    session = await create_test_session(catchall_client, name="oversized-item")
    store = SqlAlchemyConversationStore(db_uri)
    store.append(
        session["id"], [_user_item("before"), _user_item("x" * 4096), _user_item("after")]
    )
    original = SqlAlchemyConversationStore._decode_item_data_batch

    def frame_limited(self: SqlAlchemyConversationStore, stored: list[str]) -> list[str]:
        if sum(len(s) for s in stored) > 2048:
            raise _rpc_error("RESOURCE_EXHAUSTED", detail="Frame size 4300 exceeds maximum: 2048")
        return original(self, stored)

    monkeypatch.setattr(SqlAlchemyConversationStore, "_decode_item_data_batch", frame_limited)
    with caplog.at_level(logging.WARNING):
        response = await catchall_client.get(
            f"/v1/sessions/{session['id']}/items", params={"order": "asc", "limit": 100}
        )

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert [item["type"] for item in items] == ["message", "error", "message"]
    assert items[1]["code"] == "item_data_unreadable"
    assert [items[0]["content"][0]["text"], items[2]["content"][0]["text"]] == ["before", "after"]
    assert not [
        r for r in caplog.records if r.name == "omnigent.server.app" and r.levelno >= logging.ERROR
    ]
