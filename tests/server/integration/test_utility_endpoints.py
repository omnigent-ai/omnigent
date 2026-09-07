"""Integration tests for utility endpoints.

Covers ``GET /health``, ``GET /api/version``, ``GET /v1/info``,
and ``GET /v1/me``.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route-to-store
pipeline without subprocesses.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.feature_flags import Feature, FeatureFlags

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
    assert data["features"] == {
        "usage_page": False,
        "harness_install": False,
    }
    # Compatibility field for frontend builds predating the nested map.
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
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the feature on, ``/v1/info`` publishes the install allowlist.

    The SPA gates its setup offer on membership in this set, so it must carry
    the ids the route accepts — including the native spellings a session
    declares (``codex-native``), not just the bare ids.
    """
    from omnigent.onboarding.harness_install import ui_installable_harnesses

    enabled = FeatureFlags(frozenset({Feature.HARNESS_INSTALL}))
    monkeypatch.setattr(app.state, "feature_flags", enabled)
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["features"]["harness_install"] is True
    assert data["harness_install_enabled"] is True
    assert set(data["installable_harnesses"]) == set(ui_installable_harnesses())
    assert "codex-native" in data["installable_harnesses"]


# ── GET /v1/info: smart_routing_enabled ──────────────────
#
# The flag is the SPA's and the CLI's first gate: with it false, no Smart
# Routing surface exists anywhere (no top-level harness row, no per-harness
# Model option, no bundle-agent brain option), and ``--smart-routing`` is a hard
# error. It reports whether the SERVER can route at all — a host whose CLIs run
# off a personal subscription is gated separately, per harness, off the host's
# ``gateway_inference`` map.


async def test_info_reports_smart_routing_off_without_a_router(
    client: httpx.AsyncClient,
) -> None:
    """No ``routing:`` block and no Databricks provider means no routing."""
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    assert resp.json()["smart_routing_enabled"] is False


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        # Nothing initialized at all (an embedding host that never built caps).
        (None, False),
        # Caps with neither routing capability.
        (SimpleNamespace(routing_client=None, policy_llm_connection_factory=None), False),
        # A configured RoutingClient (a server ``llm:`` block, or
        # ``routing.provider=external``, or the Databricks-provider default).
        (SimpleNamespace(routing_client=object(), policy_llm_connection_factory=None), True),
        # A managed deployment supplies its own client from this factory.
        (SimpleNamespace(routing_client=None, policy_llm_connection_factory=lambda: None), True),
    ],
)
async def test_info_smart_routing_enabled_tracks_the_servers_routing_capability(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caps: object,
    expected: bool,
) -> None:
    """Either routing capability turns the flag on; neither leaves it off."""
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps, raising=False)

    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    assert resp.json()["smart_routing_enabled"] is expected


# ── GET /v1/info: smart_routing_sources ──────────────────
#
# Which router can answer, not whether routing exists. A harness whose inference
# is not AI-Gateway-backed can only be served by the built-in judge, so the SPA
# and the CLI read this to pick a source instead of hiding the surface.
# ``smart_routing_enabled`` is unchanged and stays the "is routing configured at
# all" answer.


def _external_client() -> object:
    from omnigent.server.smart_routing import ExternalRoutingClient

    return ExternalRoutingClient(base_url="https://ws.example.invalid", router_name="task_v1")


def _sources_caps(*, external: bool, local: bool, factory: bool) -> object:
    from omnigent.server.routing_backend import RoutingBackends

    backends = RoutingBackends(
        external=_external_client() if external else None,
        local=object() if local else None,
    )
    return SimpleNamespace(
        routing_client=backends.any(),
        routing_backends=backends,
        policy_llm_connection_factory=(lambda: None) if factory else None,
    )


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        # Nothing configured at all.
        (
            _sources_caps(external=False, local=False, factory=False),
            {"external": False, "oss": False},
        ),
        # The workspace AI Gateway alone: no fallback for an ungatewayed harness.
        (
            _sources_caps(external=True, local=False, factory=False),
            {"external": True, "oss": False},
        ),
        # The built-in judge alone.
        (
            _sources_caps(external=False, local=True, factory=False),
            {"external": False, "oss": True},
        ),
        # Both — the normal Databricks posture, and the only one that can serve
        # a gateway-backed AND an ungatewayed harness.
        (_sources_caps(external=True, local=True, factory=False), {"external": True, "oss": True}),
        # A managed deployment supplies its own client from the factory; that
        # counts as an OSS source, exactly as smart_routing_enabled treats it.
        (
            _sources_caps(external=False, local=False, factory=True),
            {"external": False, "oss": True},
        ),
    ],
)
async def test_info_reports_which_routers_can_answer(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caps: object,
    expected: dict[str, bool],
) -> None:
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps, raising=False)

    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    assert resp.json()["smart_routing_sources"] == expected


async def test_info_reports_routing_on_for_a_backends_only_deployment(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``routing_backends`` alone is enough — it is the managed override point.

    Regression: the flag read ``routing_client`` while the sources read the
    backends pair, so a deployment that set only ``routing_backends`` reported
    routing off and the SPA hid a surface the server would have served.
    """
    from omnigent.server.routing_backend import RoutingBackends

    monkeypatch.setattr(
        "omnigent.runtime._globals._caps",
        SimpleNamespace(
            routing_backends=RoutingBackends(external=_external_client()),
            routing_client=None,
            policy_llm_connection_factory=None,
        ),
        raising=False,
    )

    resp = await client.get("/v1/info")
    data = resp.json()
    assert data["smart_routing_enabled"] is True
    assert data["smart_routing_sources"] == {"external": True, "oss": False}


async def test_info_classifies_a_legacy_single_routing_client_as_the_oss_judge(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``routing_backends`` derives the pair from ``routing_client`` by type."""
    monkeypatch.setattr(
        "omnigent.runtime._globals._caps",
        SimpleNamespace(
            routing_client=object(),
            routing_backends=None,
            policy_llm_connection_factory=None,
        ),
        raising=False,
    )

    resp = await client.get("/v1/info")
    data = resp.json()
    assert data["smart_routing_enabled"] is True
    assert data["smart_routing_sources"] == {"external": False, "oss": True}


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
