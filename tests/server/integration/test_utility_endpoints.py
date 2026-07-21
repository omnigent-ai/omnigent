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
    resp = await client.get("/health", params={"session_id": "5ab79713d8c7904f4f4bf10b2da5df62"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "session" in data
    assert data["session"]["id"] == "5ab79713d8c7904f4f4bf10b2da5df62"
    assert "runner_online" in data["session"]


async def test_health_with_batch_session_ids(client: httpx.AsyncClient) -> None:
    """Health with comma-separated session_ids returns a sessions dict."""
    resp = await client.get(
        "/health",
        params={
            "session_ids": "94c349190e241f85a984b3df8f129696,bfcc6c068875253adf2f20bf30a19015"
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "sessions" in data
    assert "94c349190e241f85a984b3df8f129696" in data["sessions"]
    assert "bfcc6c068875253adf2f20bf30a19015" in data["sessions"]


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
    # harness_install_enabled gates the UI Install action; default off unless
    # OMNIGENT_HARNESS_INSTALL_ENABLED is set, so it's false in the test app.
    assert data["harness_install_enabled"] is False
    # installable_harnesses is the allowlist the SPA offers setup for; blank
    # while the feature is off so the UI never offers an install the disabled
    # route would reject.
    assert data["installable_harnesses"] == []
    # single_user reflects OMNIGENT_LOCAL_SINGLE_USER, which the suite's
    # conftest sets to "1" (the default local-dev posture), so it's true here.
    # The multi-user (marker-off) case is covered below.
    assert data["single_user"] is True


async def test_info_single_user_false_without_marker(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``single_user`` tracks ``OMNIGENT_LOCAL_SINGLE_USER`` live.

    It's the sole signal that separates a genuine one-user local server
    from a multi-user header-auth deploy (both otherwise report
    ``accounts_enabled: false`` / ``login_url: null``), so it must come
    from the explicit marker, not the auth shape. With the marker cleared,
    the same auth-shape app reports false — the regression this signal fixes.
    """
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["single_user"] is False
    # Auth shape is unchanged — single_user is orthogonal to it.
    assert data["accounts_enabled"] is False
    assert data["login_url"] is None


async def test_info_advertises_installable_harnesses_when_enabled(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the feature on, ``/v1/info`` publishes the install allowlist.

    The SPA gates its setup offer on membership in this set, so it must carry
    the ids the route accepts — including the native spellings a session
    declares (``codex-native``), not just the bare ids.
    """
    from omnigent.onboarding.harness_install import ui_installable_harnesses
    from omnigent.server.routes.hosts import HARNESS_INSTALL_ENABLED_ENV

    monkeypatch.setenv(HARNESS_INSTALL_ENABLED_ENV, "1")
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["harness_install_enabled"] is True
    assert set(data["installable_harnesses"]) == set(ui_installable_harnesses())
    assert "codex-native" in data["installable_harnesses"]


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
