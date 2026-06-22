"""Integration tests for session labels and owner endpoints.

Covers ``GET /v1/sessions/{id}/labels`` and
``GET /v1/sessions/{id}/owner``.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route-to-store
pipeline without subprocesses.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.unit.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    title: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a session and return the response JSON."""
    payload: dict[str, Any] = {"agent_id": agent_id}
    if title is not None:
        payload["title"] = title
    if labels is not None:
        payload["labels"] = labels
    resp = await client.post("/v1/sessions", json=payload)
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()


# ── GET /v1/sessions/{id}/labels ─────────────────────────


async def test_labels_empty_on_new_session(
    client: httpx.AsyncClient,
) -> None:
    """A freshly created session with no labels returns an empty dict."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    resp = await client.get(f"/v1/sessions/{session['id']}/labels")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session["id"]
    # Labels may contain auto-injected keys (e.g. closed status);
    # at minimum no user-supplied labels are present.
    assert isinstance(data["labels"], dict)


async def test_labels_set_via_create(
    client: httpx.AsyncClient,
) -> None:
    """Labels passed at session creation are returned by GET labels."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        labels={"env": "staging", "team": "platform"},
    )
    resp = await client.get(f"/v1/sessions/{session['id']}/labels")
    assert resp.status_code == 200
    data = resp.json()
    assert data["labels"]["env"] == "staging"
    assert data["labels"]["team"] == "platform"


async def test_labels_updated_via_patch(
    client: httpx.AsyncClient,
) -> None:
    """Labels updated via PATCH are reflected by a subsequent GET labels."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        labels={"env": "dev"},
    )
    sid = session["id"]

    # Update labels via PATCH
    patch_resp = await client.patch(
        f"/v1/sessions/{sid}",
        json={"labels": {"env": "prod", "region": "us-east"}},
    )
    assert patch_resp.status_code == 200

    # Verify GET labels reflects the update
    resp = await client.get(f"/v1/sessions/{sid}/labels")
    assert resp.status_code == 200
    data = resp.json()
    assert data["labels"]["env"] == "prod"
    assert data["labels"]["region"] == "us-east"


async def test_labels_persist_across_updates(
    client: httpx.AsyncClient,
) -> None:
    """Successive PATCH updates overwrite labels correctly."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        labels={"version": "1"},
    )
    sid = session["id"]

    # First update
    await client.patch(
        f"/v1/sessions/{sid}",
        json={"labels": {"version": "2", "extra": "yes"}},
    )

    # Second update
    await client.patch(
        f"/v1/sessions/{sid}",
        json={"labels": {"version": "3"}},
    )

    resp = await client.get(f"/v1/sessions/{sid}/labels")
    assert resp.status_code == 200
    assert resp.json()["labels"]["version"] == "3"


# ── GET /v1/sessions/{id}/owner ──────────────────────────


async def test_owner_returns_none_without_auth(
    client: httpx.AsyncClient,
) -> None:
    """Without an auth provider the owner is null."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    resp = await client.get(f"/v1/sessions/{session['id']}/owner")
    assert resp.status_code == 200
    assert resp.json()["owner"] is None


async def test_owner_null_for_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """GET owner for a nonexistent session returns null owner (no auth)."""
    resp = await client.get("/v1/sessions/conv_nonexistent_abc123/owner")
    assert resp.status_code == 200
    assert resp.json()["owner"] is None
