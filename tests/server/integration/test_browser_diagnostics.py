"""Browser observations remain bounded and scoped to the authenticated session reader."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from tests.server.integration.test_sessions_permissions import (
    _create_session_as,
)
from tests.server.integration.test_sessions_permissions import (
    auth_app as auth_app,
)
from tests.server.integration.test_sessions_permissions import (
    auth_client as auth_client,
)

pytestmark = pytest.mark.asyncio


def _batch(**event_fields: Any) -> dict[str, Any]:
    return {
        "client_instance_id": "f867d6b4-58b0-4aaa-a629-3c850f7898ed",
        "client_bundle": "index-test.js",
        "dropped_events": 2,
        "events": [
            {
                "event_name": "browser_approval_rendered",
                "sequence": 3,
                "client_time_ms": 1_700_000_000_000,
                "elicitation_id": "approval-1",
                "actionable": True,
                "tab_visible": False,
                **event_fields,
            }
        ],
    }


async def test_browser_diagnostics_auth_and_attribution(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    session = await _create_session_as(auth_client, "unused", "alice@example.com")
    url = f"/v1/sessions/{session['id']}/diagnostics"
    with caplog.at_level(
        logging.INFO, logger="omnigent.server.routes.sessions.routes_diagnostics"
    ):
        assert (await auth_client.post(url, json=_batch())).status_code == 401
        assert (
            await auth_client.post(
                url, headers={"X-Forwarded-Email": "bob@example.com"}, json=_batch()
            )
        ).status_code == 404
        response = await auth_client.post(
            url, headers={"X-Forwarded-Email": "alice@example.com"}, json=_batch()
        )
    assert response.status_code == 204
    rows = [
        row
        for row in caplog.records
        if getattr(row, "event_name", None) == "browser_approval_rendered"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.__dict__["session_id"] == session["id"]
    assert row.__dict__["user_id"] == "alice@example.com"
    assert row.__dict__["attributes"] == {
        "emitter": "browser",
        "client_instance_id": "f867d6b4-58b0-4aaa-a629-3c850f7898ed",
        "client_bundle": "index-test.js",
        "client_sequence": 3,
        "dropped_events": 2,
        "client_time_ms": 1_700_000_000_000,
        "elicitation_id": "approval-1",
        "actionable": True,
        "tab_visible": False,
    }


async def test_browser_diagnostics_rejects_foreign_target_before_logging(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    own = await _create_session_as(auth_client, "unused", "alice@example.com")
    other = await _create_session_as(auth_client, "unused", "bob@example.com")
    batch = _batch()
    batch["events"].append({**batch["events"][0], "target_session_id": other["id"], "sequence": 4})
    with caplog.at_level(
        logging.INFO, logger="omnigent.server.routes.sessions.routes_diagnostics"
    ):
        response = await auth_client.post(
            f"/v1/sessions/{own['id']}/diagnostics",
            headers={"X-Forwarded-Email": "alice@example.com"},
            json=batch,
        )
    assert response.status_code == 404
    assert not any(
        getattr(row, "event_name", None) == "browser_approval_rendered" for row in caplog.records
    )


async def test_browser_diagnostics_preserves_snapshot_reconciliation_trigger(
    auth_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    session = await _create_session_as(auth_client, "unused", "alice@example.com")
    batch = _batch()
    batch["events"] = [
        {
            "event_name": "browser_status_reconnect",
            "sequence": sequence,
            "client_time_ms": 1_700_000_000_000,
            "observation_source": "snapshot",
            "observation_trigger": trigger,
            "status": "idle",
        }
        for sequence, trigger in enumerate(("periodic_reconcile", "stream_reconnect"), start=1)
    ]
    with caplog.at_level(
        logging.INFO, logger="omnigent.server.routes.sessions.routes_diagnostics"
    ):
        response = await auth_client.post(
            f"/v1/sessions/{session['id']}/diagnostics",
            headers={"X-Forwarded-Email": "alice@example.com"},
            json=batch,
        )
    assert response.status_code == 204
    assert [
        row.__dict__["attributes"]["observation_trigger"]
        for row in caplog.records
        if getattr(row, "event_name", None) == "browser_status_reconnect"
    ] == ["periodic_reconcile", "stream_reconnect"]


@pytest.mark.parametrize(
    "fields",
    [
        {"prompt": "sensitive text"},
        {"user_id": "spoofed@example.com"},
        {"event_name": "resolve_elicitation"},
        {"blocked_on": "arbitrary dialog contents"},
        {"elicitation_id": "x" * 129},
        {"actionable": "true"},
        {"observation_trigger": "arbitrary trigger"},
    ],
)
async def test_browser_diagnostics_rejects_unbounded_or_spoofed_fields(
    auth_client: httpx.AsyncClient, fields: dict[str, Any]
) -> None:
    session = await _create_session_as(auth_client, "unused", "alice@example.com")
    response = await auth_client.post(
        f"/v1/sessions/{session['id']}/diagnostics",
        headers={"X-Forwarded-Email": "alice@example.com"},
        json=_batch(**fields),
    )
    assert response.status_code == 422
    assert "sensitive text" not in response.text


async def test_browser_diagnostics_bounds_encoded_body_and_batch(
    auth_client: httpx.AsyncClient,
) -> None:
    session = await _create_session_as(auth_client, "unused", "alice@example.com")
    url = f"/v1/sessions/{session['id']}/diagnostics"
    headers = {"X-Forwarded-Email": "alice@example.com"}
    batch = _batch()
    batch["events"] *= 21
    assert (await auth_client.post(url, headers=headers, json=batch)).status_code == 422

    async def chunks():
        yield b" " * 20_000
        yield b" " * 20_000

    assert (await auth_client.post(url, headers=headers, content=chunks())).status_code == 413
