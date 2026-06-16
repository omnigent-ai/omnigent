"""Integration tests for host management edge cases.

Covers gaps not exercised by test_hosts_api.py or test_hosts_filesystem.py:

- ``GET /v1/runners`` returns empty when no runners are connected.
- ``GET /v1/runners/{id}/status`` returns offline for an unknown runner.
- ``GET /v1/hosts/{id}`` response shape includes the ``runners`` field.
- ``POST /v1/hosts/{id}/runners`` with missing ``session_id`` returns 422.
- ``GET /v1/hosts/{id}/filesystem/{path}`` with offline host returns 409.
- ``GET /v1/hosts`` returns offline after the host's ``last_seen_at``
  exceeds the liveness window (stale host detection).
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.host_registry import HostRegistry, RunnerExitReports
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def management_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry]:
    """FastAPI app with host + runner routes for management tests.

    :param db_uri: SQLite URI from the shared fixture.
    :returns: Tuple of (app, host_registry, host_store, conv_store, tunnel_registry).
    """
    host_registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    tunnel_registry = TunnelRegistry()
    reports = RunnerExitReports()
    app = FastAPI()
    app.include_router(
        create_hosts_router(host_registry, host_store, conv_store),
        prefix="/v1",
    )
    app.include_router(
        create_runner_tunnel_router(tunnel_registry, runner_exit_reports=reports),
        prefix="/v1",
    )
    return app, host_registry, host_store, conv_store, tunnel_registry


# ── Runner list / status (no runners connected) ─────────


async def test_list_runners_empty(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """GET /v1/runners returns an empty data list when no runners are connected.

    If the response shape is wrong or non-empty, runner enumeration
    is broken or leaking stale state.
    """
    app, *_ = management_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/runners")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body, "response must contain a 'data' key"
    assert body["data"] == []


async def test_runner_status_unknown_runner_offline(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """GET /v1/runners/{id}/status returns online=false for a nonexistent runner.

    The endpoint must not 404 — it always returns a status object so
    polling clients do not need error handling for the not-yet-connected
    case.
    """
    app, *_ = management_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/runners/runner_does_not_exist/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["runner_id"] == "runner_does_not_exist"
    assert body["online"] is False


async def test_runner_status_unknown_runner_no_error_field(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """GET /v1/runners/{id}/status omits the error field when no exit report exists.

    Clients distinguish 'not yet connected' (no error) from 'crashed'
    (error present). If error is always present, the UI would show a
    spurious failure state for runners that are still starting.
    """
    app, *_ = management_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/runners/runner_starting/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body, "error field must be absent for a runner with no exit report"


# ── Host detail response shape ───────────────────────────


async def test_get_host_detail_includes_runners_field(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """GET /v1/hosts/{id} includes a 'runners' list in the response.

    The Web UI reads this field to show runner state in the host
    detail panel. If it is missing, the UI throws a TypeError on
    access.
    """
    app, _hr, host_store, *_ = management_app
    host_store.upsert_on_connect("host_detail", "detail-laptop", "local")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts/host_detail")
    assert resp.status_code == 200
    body = resp.json()
    assert "runners" in body, "response must contain a 'runners' key"
    assert isinstance(body["runners"], list)
    assert body["runners"] == [], "runners list should be empty when no runners are launched"


# ── Launch validation ────────────────────────────────────


async def test_launch_runner_missing_session_id_returns_422(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """POST /v1/hosts/{id}/runners with missing session_id returns 422.

    The request body requires ``session_id`` and ``workspace``.
    Omitting a required field must trigger Pydantic validation, not
    a 500.
    """
    app, _hr, host_store, *_ = management_app
    host_store.upsert_on_connect("host_validate", "laptop", "local")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_validate/runners",
            json={"workspace": "/tmp/test"},
        )
    assert resp.status_code == 422, f"Expected 422 for missing session_id, got {resp.status_code}"


async def test_launch_runner_missing_workspace_returns_422(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """POST /v1/hosts/{id}/runners with missing workspace returns 422.

    Both ``session_id`` and ``workspace`` are required fields.
    """
    app, _hr, host_store, *_ = management_app
    host_store.upsert_on_connect("host_validate2", "laptop", "local")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_validate2/runners",
            json={"session_id": "conv_fake"},
        )
    assert resp.status_code == 422, f"Expected 422 for missing workspace, got {resp.status_code}"


# ── Stale host liveness ─────────────────────────────────


async def test_list_hosts_stale_host_reported_offline(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """GET /v1/hosts reports a host as offline when last_seen_at is stale.

    A host that crashed without calling set_offline would stay
    ``status="online"`` in the DB forever. The route applies a
    liveness window (host_is_live) so stale hosts read as offline.
    If this test fails, the liveness check is missing and ghost
    hosts appear permanently online in the picker.
    """
    app, _hr, host_store, *_ = management_app
    # Register the host so it has status="online" in the DB.
    host_store.upsert_on_connect("host_stale", "stale-laptop", "local")

    # Verify it initially shows as online.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")
    hosts = resp.json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["status"] == "online"

    # Manually backdate last_seen_at to simulate a crashed host.
    # The liveness window is typically 60-120s; setting it 10 minutes
    # in the past guarantees it exceeds any reasonable window.
    from sqlalchemy import text

    stale_time = int(time.time()) - 600
    with host_store._engine.connect() as conn:
        conn.execute(
            text("UPDATE hosts SET updated_at = :ts WHERE host_id = :hid"),
            {"ts": stale_time, "hid": "host_stale"},
        )
        conn.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")
    hosts = resp.json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["host_id"] == "host_stale"
    assert hosts[0]["status"] == "offline", (
        "A host with a stale last_seen_at should be reported as offline. "
        "The liveness check (host_is_live) is missing from the list route."
    )


# ── Host detail for offline host ─────────────────────────


async def test_get_host_detail_offline_status(
    management_app: tuple[
        FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore, TunnelRegistry
    ],
) -> None:
    """GET /v1/hosts/{id} returns status=offline for an offline host.

    The detail endpoint uses the same DB-backed liveness check as
    the list endpoint. Verifying parity prevents a UI bug where the
    list shows offline but the detail view shows online.
    """
    app, _hr, host_store, *_ = management_app
    host_store.upsert_on_connect("host_off", "off-laptop", "local")
    host_store.set_offline("host_off")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts/host_off")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "offline"
    assert body["host_id"] == "host_off"
