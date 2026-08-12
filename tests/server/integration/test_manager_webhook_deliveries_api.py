"""Integration tests for ``GET /v1/sessions/{id}/manager-webhook-deliveries``.

Covers the "attempts/outbox status in API" acceptance requirement (OMN-104
design doc §9) — the read-only observability endpoint modeled on
``GET /scheduled-tasks/{id}/runs``. Uses the shared no-auth ``client``/``app``
fixtures (real stores + mock LLM) from ``tests/server/conftest.py``, matching
``tests/server/integration/test_sessions_elicitation_api.py``'s pattern.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from fastapi import FastAPI

from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_elicitation_resolve_url import _create_session

pytestmark = pytest.mark.asyncio


async def _make_session(client: httpx.AsyncClient) -> str:
    agent = await create_test_agent(client, "test-manager-webhook-deliveries")
    return await _create_session(client, agent["id"])


def _insert_event(
    app: FastAPI,
    session_id: str,
    *,
    event_type: str,
    transition_key: str,
    sequence_marker: str,
    now: int = 1_000_000,
) -> str:
    """Insert one outbox row directly via the app-wide store; returns its event_id."""
    store = app.state.session_lifecycle_store
    # event_id must be a bare 32-hex-char Uuid16 value; derive a deterministic
    # one from the inputs (the store accepts any caller-supplied event_id).
    event_id = hashlib.sha256(
        f"{session_id}:{transition_key}:{sequence_marker}".encode()
    ).hexdigest()[:32]
    store.record_lifecycle_event(
        event_id=event_id,
        session_id=session_id,
        event_type=event_type,
        transition_key=transition_key,
        payload=json.dumps({"marker": sequence_marker}),
        now=now,
    )
    return event_id


async def test_no_deliveries_returns_empty_list(client: httpx.AsyncClient) -> None:
    """A session with no manager-webhook activity returns an empty page, not a 404/500."""
    session_id = await _make_session(client)
    resp = await client.get(f"/v1/sessions/{session_id}/manager-webhook-deliveries")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deliveries": [], "next_cursor": None}


async def test_populated_deliveries_ordered_most_recent_first(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Rows come back highest-sequence-first, with the documented field shape."""
    session_id = await _make_session(client)
    _insert_event(
        app,
        session_id,
        event_type="session.completed",
        transition_key="turn:r1:completed",
        sequence_marker="a",
    )
    _insert_event(
        app,
        session_id,
        event_type="session.failed",
        transition_key="turn:r2:failed",
        sequence_marker="b",
    )

    resp = await client.get(f"/v1/sessions/{session_id}/manager-webhook-deliveries")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    deliveries = body["deliveries"]
    assert len(deliveries) == 2
    # Most recent (highest sequence, i.e. the second insert) first.
    assert deliveries[0]["event_type"] == "session.failed"
    assert deliveries[0]["sequence"] == 2
    assert deliveries[1]["event_type"] == "session.completed"
    assert deliveries[1]["sequence"] == 1
    # Status is the decoded string name, not a raw int code.
    assert deliveries[0]["status"] == "pending"
    for row in deliveries:
        assert set(row) == {
            "event_id",
            "event_type",
            "sequence",
            "status",
            "attempt_count",
            "last_attempt_at",
            "delivered_at",
            "last_http_status",
            "last_error_code",
            "last_error_message",
        }


async def test_pagination_no_gaps_or_dupes(client: httpx.AsyncClient, app: FastAPI) -> None:
    """Cursor pagination walks every row exactly once across pages."""
    session_id = await _make_session(client)
    for i in range(5):
        _insert_event(
            app,
            session_id,
            event_type="session.completed",
            transition_key=f"turn:r{i}:completed",
            sequence_marker=str(i),
        )

    seen_sequences: list[int] = []
    cursor: str | None = None
    for _ in range(10):  # bounded loop guard
        params = {"limit": 2}
        if cursor is not None:
            params["after"] = cursor
        resp = await client.get(
            f"/v1/sessions/{session_id}/manager-webhook-deliveries", params=params
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen_sequences.extend(d["sequence"] for d in body["deliveries"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("pagination did not terminate within 10 pages")

    assert sorted(seen_sequences) == [1, 2, 3, 4, 5]
    assert len(seen_sequences) == len(set(seen_sequences))


async def test_nonexistent_session_returns_404(client: httpx.AsyncClient) -> None:
    """A session id that doesn't exist 404s rather than 500ing or returning an empty page."""
    resp = await client.get("/v1/sessions/conv_does_not_exist/manager-webhook-deliveries")
    assert resp.status_code == 404, resp.text


async def test_store_unwired_degrades_gracefully() -> None:
    """``_get_session_lifecycle_store`` returns ``None`` (not an error) off a bare Request-like
    app.state that never had the store attached — the route's own getattr fallback."""
    from omnigent.server.routes.sessions.routes_manager_webhook import (
        _get_session_lifecycle_store,
    )

    class _FakeState:
        pass

    class _FakeRequest:
        app = type("_FakeApp", (), {"state": _FakeState()})()

    assert _get_session_lifecycle_store(_FakeRequest()) is None  # type: ignore[arg-type]
