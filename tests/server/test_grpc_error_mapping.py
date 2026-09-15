"""The app maps backend gRPC access denials to handled 403/401 responses.

A store backend reached over gRPC (e.g. a workspace hierarchy service behind
a channel proxy) can answer a request with ``PERMISSION_DENIED`` whose details
read ``"Received http2 header with status: 403"``. Without a typed handler,
that ``grpc.RpcError`` matched only the bare ``Exception`` catch-all, so the
user got a 500 ``internal_error`` (with a full traceback logged per client
retry) instead of an actionable 403. These tests pin the mapping at the app
layer: access denials become handled 403/401 responses naming the resource,
and every other gRPC status keeps the unhandled-500 contract.
"""

from __future__ import annotations

import grpc
import httpx
import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# The exact details string a proxied HTTP 403 surfaces as on the gRPC channel.
_DENIAL_DETAILS = "Received http2 header with status: 403"


class _BackendRpcError(grpc.RpcError):
    """A gRPC error exposing the ``code()``/``details()`` of a failed call."""

    def __init__(self, status: grpc.StatusCode, details: str) -> None:
        super().__init__()
        self._status = status
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._status

    def details(self) -> str:
        return self._details


def _fail_listing_with(
    monkeypatch: pytest.MonkeyPatch,
    status: grpc.StatusCode,
    details: str,
) -> None:
    """Make every conversation listing raise a gRPC error.

    Stands in for a workspace-hierarchy-backed store whose backend denies the
    listing RPC: the error is raised from inside ``GET /v1/sessions``, the
    funnel every sidebar session-list query goes through.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param status: gRPC status code the backend answers with.
    :param details: gRPC details string carried by the error.
    """

    def _raise(self: SqlAlchemyConversationStore, *args: object, **kwargs: object) -> object:
        raise _BackendRpcError(status, details)

    monkeypatch.setattr(SqlAlchemyConversationStore, "list_conversations", _raise)


async def test_grpc_permission_denied_maps_to_403(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend ``PERMISSION_DENIED`` answers a handled 403, not a raw 500."""
    _fail_listing_with(monkeypatch, grpc.StatusCode.PERMISSION_DENIED, _DENIAL_DETAILS)
    resp = await client.get("/v1/sessions", params={"limit": 30})
    assert resp.status_code == 403
    error = resp.json()["error"]
    assert error["code"] == "forbidden"
    # The message names the denied resource so the user has something to act on.
    assert "/v1/sessions" in error["message"]


async def test_grpc_unauthenticated_maps_to_401(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend ``UNAUTHENTICATED`` answers a handled 401."""
    _fail_listing_with(monkeypatch, grpc.StatusCode.UNAUTHENTICATED, "bad credentials")
    resp = await client.get("/v1/sessions", params={"limit": 30})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_other_grpc_errors_keep_the_500_contract(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-denial gRPC failure still surfaces as the standard 500 shape."""
    _fail_listing_with(monkeypatch, grpc.StatusCode.UNAVAILABLE, "connection refused")
    resp = await client.get("/v1/sessions", params={"limit": 30})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
