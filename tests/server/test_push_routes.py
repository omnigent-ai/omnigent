"""Tests for the Web Push subscription routes (#8) — SSRF guard + owner-scoping."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.push import create_push_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.push_subscription_store.sqlalchemy_store import (
    SqlAlchemyPushSubscriptionStore,
)


def _app(router: APIRouter) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    app.include_router(router, prefix="/v1")
    return app


@pytest.mark.parametrize(
    "endpoint",
    ["http://push.example.com/x", "https://127.0.0.1/x", "https://169.254.169.254/x"],
)
def test_subscribe_rejects_ssrf_endpoints(db_uri: str, endpoint: str) -> None:
    # The server would later POST to this endpoint, so a non-https or
    # internal-address endpoint is rejected before it's persisted.
    store = SqlAlchemyPushSubscriptionStore(db_uri)
    client = TestClient(_app(create_push_router(store)))
    res = client.post(
        "/v1/push/subscriptions",
        json={"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}},
    )
    assert res.status_code == 400


def test_unsubscribe_is_scoped_to_owner(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # A user can delete only their OWN subscription — not another user's by
    # replaying its endpoint. (Validation is stubbed; that's covered elsewhere.)
    monkeypatch.setattr("omnigent.server.routes.push.validate_push_endpoint", lambda _e: None)
    store = SqlAlchemyPushSubscriptionStore(db_uri)
    perm = SqlAlchemyPermissionStore(db_uri)
    for user in ("alice@example.com", "bob@example.com"):
        perm.ensure_user(user)
    router = create_push_router(
        store,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
        permission_store=perm,
    )
    client = TestClient(_app(router))
    alice = {"X-Forwarded-Email": "alice@example.com"}
    bob = {"X-Forwarded-Email": "bob@example.com"}
    endpoint = "https://push.example.com/alice-sub"

    assert (
        client.post(
            "/v1/push/subscriptions",
            json={"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}},
            headers=alice,
        ).status_code
        == 200
    )
    # Bob can't delete alice's subscription; alice can.
    assert client.request(
        "DELETE", "/v1/push/subscriptions", json={"endpoint": endpoint}, headers=bob
    ).json() == {"deleted": False}
    assert client.request(
        "DELETE", "/v1/push/subscriptions", json={"endpoint": endpoint}, headers=alice
    ).json() == {"deleted": True}
