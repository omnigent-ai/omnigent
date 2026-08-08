"""Tests for the helpers both OAuth grant routers share.

:mod:`omnigent.server.routes._oauth` holds the pieces the device grant and the
client-credentials grant answer with identically: the RFC 6749 §5.1 no-store
header pair, the error-response factory that carries it, and the per-key
sliding-window throttle. What is covered here is what a single shared object
makes possible to get wrong once for both grants — a mutated constant, or an
RFC 8628 §3.2 response that leaks its ``device_code`` into a cache.

The grant-specific behaviour lives in ``test_device_auth.py`` and
``test_client_credentials.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.routes._oauth import (
    NO_STORE_HEADERS,
    SlidingWindowRateLimiter,
    oauth_error,
)
from omnigent.server.routes.device_auth import create_device_auth_router

_COOKIE_SECRET = b"d" * 32


# ── The no-store header constant ──────────────────────────────────


def test_no_store_headers_are_immutable() -> None:
    """The documented constant cannot be edited by a caller.

    One mapping is handed to responses from both grant routers, so a mutable
    one would let any caller silently re-header every later token response in
    the process.
    """
    with pytest.raises(TypeError):
        NO_STORE_HEADERS["Cache-Control"] = "public"  # type: ignore[index]
    with pytest.raises(TypeError):
        NO_STORE_HEADERS["X-Added"] = "1"  # type: ignore[index]
    assert dict(NO_STORE_HEADERS) == {"Cache-Control": "no-store", "Pragma": "no-cache"}


def test_oauth_error_carries_the_no_store_pair_and_merges_extras() -> None:
    """Extra headers merge over the pair without touching the constant."""
    response = oauth_error(
        "invalid_client", status_code=401, headers={"WWW-Authenticate": "Basic"}
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["www-authenticate"] == "Basic"
    assert dict(NO_STORE_HEADERS) == {"Cache-Control": "no-store", "Pragma": "no-cache"}


# ── The shared throttle ───────────────────────────────────────────


def test_sliding_window_allows_up_to_the_ceiling_then_refuses() -> None:
    limiter = SlidingWindowRateLimiter(3, 60, 10)
    assert [limiter.allow("ip", 1000.0) for _ in range(4)] == [True, True, True, False]
    # A key whose hits have aged out is admitted again.
    assert limiter.allow("ip", 1061.0) is True


# ── RFC 8628 §3.2: the device authorize response ──────────────────


def _device_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Mount only the device-grant router, with no client secret configured."""
    monkeypatch.delenv("OMNIGENT_DEVICE_CLIENT_SECRET", raising=False)
    provider = UnifiedAuthProvider(
        source="accounts",
        accounts_config=AccountsConfig(
            cookie_secret=_COOKIE_SECRET,
            session_ttl_hours=8,
            base_url="http://localhost:8000",
            init_admin_password=None,
            invite_ttl_seconds=3600,
            magic_ttl_seconds=300,
        ),
    )
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    app = FastAPI()
    app.include_router(create_device_auth_router(provider, store))
    return TestClient(app)


def test_device_authorize_response_is_not_cacheable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authorize 200 carries ``device_code`` + ``user_code``, so no-store.

    RFC 8628 §3.2's body is as sensitive as a token response: the device_code is
    the bearer credential the client later redeems.
    """
    client = _device_app(tmp_path, monkeypatch)
    resp = client.post("/oauth/device/authorize", json={"client_id": "cli"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["device_code"]
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
