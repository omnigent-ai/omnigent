"""Integration tests for utility endpoints.

Covers ``GET /health``, ``GET /api/version``, ``GET /v1/info``,
and ``GET /v1/me``.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route-to-store
pipeline without subprocesses.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.asyncio


# ── GET /health ──────────────────────────────────────────


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    """Bare liveness probe returns 200 with status ok."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


async def test_health_with_session_id(client: httpx.AsyncClient) -> None:
    """Health with a session_id query param includes a session object."""
    resp = await client.get("/health", params={"session_id": "conv_fake"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "session" in data
    assert data["session"]["id"] == "conv_fake"
    assert "runner_online" in data["session"]


async def test_health_with_batch_session_ids(client: httpx.AsyncClient) -> None:
    """Health with comma-separated session_ids returns a sessions dict."""
    resp = await client.get(
        "/health",
        params={"session_ids": "conv_a,conv_b"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "sessions" in data
    assert "conv_a" in data["sessions"]
    assert "conv_b" in data["sessions"]


# ── GET /api/version ─────────────────────────────────────


async def test_version_returns_string(client: httpx.AsyncClient) -> None:
    """Version endpoint returns a version string."""
    resp = await client.get("/api/version")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0


# ── GET /v1/info ─────────────────────────────────────────


async def test_info_returns_expected_fields(client: httpx.AsyncClient) -> None:
    """Info endpoint returns auth mode and feature flags."""
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    data = resp.json()
    # The test app has no auth provider, so accounts are disabled.
    assert data["accounts_enabled"] is False
    assert data["login_url"] is None
    assert data["needs_setup"] is False
    assert isinstance(data["databricks_features"], bool)
    assert isinstance(data["managed_sandboxes_enabled"], bool)


# ── GET /v1/me ───────────────────────────────────────────


async def test_me_returns_null_user_without_auth(client: httpx.AsyncClient) -> None:
    """Without auth, /v1/me returns user_id null and is_admin false."""
    resp = await client.get("/v1/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] is None
    # is_admin is always present (mode-agnostic admin signal) and false
    # for an unauthenticated / no-permission-store caller.
    assert data["is_admin"] is False
