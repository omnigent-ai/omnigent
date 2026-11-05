"""HTTP tests for ``/v1/sessions/{id}/objectives``: recording, listing and access."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import _is_unrouted
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient
from tests.server.integration.test_scheduled_tasks_routes import (
    _build_app,
    _headers,
    _make_user,
    _serve,
)

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    app = _build_app(db_uri, tmp_path)
    _make_user(db_uri, ALICE)
    _make_user(db_uri, BOB)
    _make_user(db_uri, CAROL)
    return app


@pytest_asyncio.fixture()
async def client(
    app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(app, mock_llm, tmp_path) as client:
        yield client


def _session(db_uri: str, *, labels: dict[str, str] | None = None) -> str:
    """A session alice owns."""
    conversations = SqlAlchemyConversationStore(db_uri)
    session = conversations.create_conversation()
    SqlAlchemyPermissionStore(db_uri).grant(ALICE, session.id, 4)
    if labels:
        conversations.set_labels(session.id, labels)
    return session.id


def _outcome(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"success": True, "emoji": "✅", "text": "Done"}
    body.update(overrides)
    return body


async def test_objectives_and_outcomes_are_recorded_and_listed_in_order(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/objectives"
    first = await client.post(url, json={"objective": "Fix the flaky test"}, headers=_headers())
    assert first.status_code == 200, first.text
    second = await client.post(url, json={"objective": "Ship it"}, headers=_headers())
    resolved = await client.put(
        f"{url}/outcome",
        json=_outcome(success=False, emoji=" 🚧 ", text="Blocked:  CI"),
        headers=_headers(),
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    # Without objective_id, the latest objective is resolved.
    assert body["id"] == second.json()["id"]
    assert body["outcome"]["success"] is False
    assert (body["outcome"]["emoji"], body["outcome"]["text"]) == ("🚧", "Blocked: CI")
    assert isinstance(body["outcome"]["recorded_at"], int)
    assert body["scheduled_task_run_id"] is None

    named = await client.put(
        f"{url}/outcome",
        json=_outcome(objective_id=first.json()["id"], emoji="🚀", text="Fixed"),
        headers=_headers(),
    )
    assert named.json()["id"] == first.json()["id"]

    listed = await client.get(url, headers=_headers())
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert [(o["objective"], o["outcome"]["text"]) for o in data] == [
        ("Fix the flaky test", "Fixed"),
        ("Ship it", "Blocked: CI"),
    ]
    assert listed.json()["next_cursor"] is None
    page = await client.get(url, params={"limit": 1}, headers=_headers())
    rest = await client.get(
        url, params={"limit": 1, "cursor": page.json()["next_cursor"]}, headers=_headers()
    )
    assert [o["id"] for o in page.json()["data"] + rest.json()["data"]] == [o["id"] for o in data]


async def test_what_is_recorded_during_a_run_is_attributed_to_it(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    run_id = uuid.uuid4().hex
    session_id = _session(db_uri, labels={SCHEDULED_TASK_RUN_LABEL_KEY: run_id})
    url = f"/v1/sessions/{session_id}/objectives"
    objective = await client.post(url, json={"objective": "Triage"}, headers=_headers())
    outcome = await client.put(f"{url}/outcome", json=_outcome(), headers=_headers())
    assert objective.json()["scheduled_task_run_id"] == run_id
    assert outcome.json()["id"] == objective.json()["id"]

    # The next run's prompt restamps the session: an outcome there leaves the
    # earlier run's objective alone.
    next_run = uuid.uuid4().hex
    SqlAlchemyConversationStore(db_uri).set_labels(
        session_id, {SCHEDULED_TASK_RUN_LABEL_KEY: next_run}
    )
    later = await client.put(
        f"{url}/outcome", json=_outcome(success=False, text="API down"), headers=_headers()
    )
    assert later.json()["id"] != objective.json()["id"]
    assert later.json()["scheduled_task_run_id"] == next_run
    data = (await client.get(url, headers=_headers())).json()["data"]
    assert [o["outcome"]["text"] for o in data] == ["Done", "API down"]


async def test_a_relayed_call_records_only_for_its_own_session(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id, other = _session(db_uri), _session(db_uri)
    url = f"/v1/sessions/{session_id}/objectives"
    for method, path, body in (
        (client.post, url, {"objective": "Goal"}),
        (client.put, f"{url}/outcome", _outcome()),
    ):
        refused = await method(
            path, json=body, headers={**_headers(), ORCHESTRATION_SOURCE_HEADER: other}
        )
        assert refused.status_code == 403, refused.text
        own = await method(
            path, json=body, headers={**_headers(), ORCHESTRATION_SOURCE_HEADER: session_id}
        )
        assert own.status_code == 200, own.text


async def test_reading_needs_read_access_and_recording_needs_edit(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/objectives"
    assert (await client.post(url, json={"objective": "Goal"}, headers=_headers())).is_success
    bob = _headers(BOB)
    assert (await client.get(url, headers=bob)).status_code in (403, 404)

    # Grants are cached per server, so each user's level is set before first use.
    permissions = SqlAlchemyPermissionStore(db_uri)
    readable = _session(db_uri)
    permissions.grant(BOB, readable, LEVEL_READ)
    permissions.grant(CAROL, readable, LEVEL_EDIT)
    url = f"/v1/sessions/{readable}/objectives"
    assert (await client.get(url, headers=bob)).status_code == 200
    assert (await client.post(url, json={"objective": "Mine"}, headers=bob)).status_code == 403
    assert (await client.put(f"{url}/outcome", json=_outcome(), headers=bob)).status_code == 403
    carol = _headers(CAROL)
    assert (await client.put(f"{url}/outcome", json=_outcome(), headers=carol)).status_code == 200


@pytest.mark.parametrize(
    ("path", "content", "status"),
    [
        ("", '{"objective": "a\\ud800"}', 422),
        ("/outcome", '{"success": true, "emoji": "ok", "text": "Done"}', 422),
        ("/outcome", '{"success": true, "emoji": "✅", "text": "a\\ud800"}', 422),
        (
            "/outcome",
            '{"success": true, "emoji": "✅", "text": "x", "objective_id": "' + "a" * 32 + '"}',
            404,
        ),
    ],
)
async def test_invalid_requests_are_refused_without_writing(
    client: httpx.AsyncClient, db_uri: str, path: str, content: str, status: int
) -> None:
    """A lone surrogate in raw JSON is a 422 like any other invalid value, not a 500.

    The request models' own tests cover the other invalid values.
    """
    session_id = _session(db_uri)
    url = f"/v1/sessions/{session_id}/objectives"
    resp = await client.request(
        "PUT" if path else "POST",
        url + path,
        content=content.encode(),
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == status, resp.text
    assert (await client.get(url, headers=_headers())).json()["data"] == []


async def test_a_missing_session_is_not_found(client: httpx.AsyncClient) -> None:
    url = f"/v1/sessions/{uuid.uuid4().hex}/objectives"
    assert (await client.get(url, headers=_headers())).status_code == 404
    assert (await client.post(url, json={"objective": "x"}, headers=_headers())).status_code == 404


async def test_runners_can_tell_an_older_server_from_a_routes_own_404(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """The runner reads an unrouted 404 as an older server, and a route's 404 as itself."""
    unrouted = await client.post(
        f"/v1/sessions/{_session(db_uri)}/no-such-route", json={}, headers=_headers()
    )
    assert unrouted.status_code == 404
    assert _is_unrouted(unrouted)
    for routed in (
        await client.put(
            f"/v1/sessions/{_session(db_uri)}/objectives/outcome",
            json=_outcome(objective_id="a" * 32),
            headers=_headers(),
        ),
        await client.post(
            f"/v1/sessions/{uuid.uuid4().hex}/objectives",
            json={"objective": "x"},
            headers=_headers(),
        ),
    ):
        assert routed.status_code == 404
        assert not _is_unrouted(routed)


async def test_clients_cannot_set_the_run_label(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = _session(db_uri)
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {SCHEDULED_TASK_RUN_LABEL_KEY: uuid.uuid4().hex}},
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text
    assert "server-internal" in resp.text
