"""Tests for the Phase 1 runner scaffold.

Verifies:
- ``create_runner_app`` returns a fresh, mountable FastAPI instance
  every call (the SERVER_HARNESS_CONTRACT.md:609-619 anti-recursion
  rule depends on this).
- ``GET /health`` returns 200 ``{"status": "ok"}`` via the
  ASGI app boundary.
- The stubbed response endpoints return 501 with a structured error
  body (so callers see "not implemented" rather than 404 or a
  surprise 200).

These tests don't validate execution logic; that lands when Phase 1
(full) extracts the tool resolver / MCP / harness lifecycle into
the runner. The scaffold's job is to prove the boundary works.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from tests.unit.runner.helpers import NullServerClient

# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture
def runner_app() -> FastAPI:
    """A fresh runner app per test — no shared state between tests."""
    return create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]


@pytest.fixture
async def runner_client(runner_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client routing through the runner app for tests."""
    transport = httpx.ASGITransport(app=runner_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


# ── App factory ─────────────────────────────────────────


def test_create_runner_app_returns_fresh_instance_each_call() -> None:
    """Each ``create_runner_app()`` call returns a NEW FastAPI app.

    The SERVER_HARNESS_CONTRACT.md:609-619 anti-recursion rule says
    the runner app object MUST NOT equal the Omnigent app object. Since
    we don't have visibility into the Omnigent app from this test, we
    verify the weaker but sufficient property: distinct calls to
    the factory yield distinct apps. If the factory ever started
    returning a cached singleton, this test would fail and signal
    that the recursion-guard invariant is at risk.
    """
    app1 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    app2 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    assert app1 is not app2, (
        "create_runner_app() must return a fresh FastAPI instance "
        "each call; sharing one app would violate the harness-contract "
        "anti-recursion rule (SERVER_HARNESS_CONTRACT.md:609-619)."
    )


# ── ASGI app round trip ─────────────────────────────────


@pytest.mark.asyncio
async def test_health_endpoint_round_trip(runner_client: httpx.AsyncClient) -> None:
    """``GET /health`` round-trips through the runner app.

    This is the load-bearing assertion for Phase 1 scaffold: it
    proves the app factory mounts a real health handler with the
    expected response shape.
    """
    response = await runner_client.get("/health")
    # Both the status code AND the body matter: a wrong handler
    # returning 200 with a different shape would otherwise pass.
    assert response.status_code == 200, (
        f"Expected 200 from /health, got {response.status_code} with body {response.text!r}"
    )
    assert response.json() == {"status": "ok"}


# ── Stub endpoints ──────────────────────────────────────


@pytest.mark.asyncio
async def test_elicitation_reply_returns_501_stub(
    runner_client: httpx.AsyncClient,
) -> None:
    """Elicitation-reply endpoint stub returns 501."""
    response = await runner_client.post(
        "/v1/elicitations/elicit_test", json={"action": "accept", "content": {}}
    )
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "not_implemented"


@pytest.mark.asyncio
async def test_unknown_path_returns_404(runner_client: httpx.AsyncClient) -> None:
    """Endpoints we deliberately don't expose return 404, not 501.

    The runner's harness API subset is intentionally narrow — it does
    NOT implement ``/v1/conversations``, ``/v1/files``, listing,
    search, etc. (per SERVER_HARNESS_CONTRACT.md:601-604). A client
    accidentally hitting those paths must get 404, not the 501
    stub-suggesting-future-work that we use for the in-scope endpoints.
    """
    response = await runner_client.get("/v1/conversations")
    assert response.status_code == 404, (
        f"Out-of-scope endpoint should 404 (telling the caller it "
        f"doesn't exist on the runner), not 501. Got {response.status_code}."
    )
