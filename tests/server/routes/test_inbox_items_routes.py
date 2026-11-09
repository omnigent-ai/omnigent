"""Tests for the ``/v1/inbox/items`` routes on a single-user server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.app import create_app
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes.inbox_items import MAX_ITEMS_PER_SESSION
from omnigent.spec.types import ToolGroupsConfig
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore

_INBOX_AGENT = "1" * 32
_PLAIN_AGENT = "2" * 32
_BUNDLE_HASH = "c" * 64


class _AgentCacheStub:
    """Loads a spec whose ``user_inbox`` group is on only for the Inbox agent."""

    def load(self, agent_id: str, bundle_location: str, **_: Any) -> SimpleNamespace:
        groups = ToolGroupsConfig(user_inbox=agent_id == _INBOX_AGENT)
        return SimpleNamespace(spec=SimpleNamespace(tool_groups=groups))


@pytest.fixture()
def stores(db_uri: str) -> SimpleNamespace:
    return SimpleNamespace(
        agents=SqlAlchemyAgentStore(db_uri),
        conversations=SqlAlchemyConversationStore(db_uri),
        inbox=SqlAlchemyInboxItemStore(db_uri),
    )


def _app(stores: SimpleNamespace, tmp_path: Path, *, enabled: bool) -> FastAPI:
    return create_app(
        agent_store=stores.agents,
        file_store=SqlAlchemyFileStore(stores.conversations.storage_location),
        conversation_store=stores.conversations,
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        agent_cache=_AgentCacheStub(),  # type: ignore[arg-type]
        inbox_item_store=stores.inbox,
        feature_flags=resolve_feature_flags(
            {"OMNIGENT_FEATURES": "proactive_agent" if enabled else ""}
        ),
    )


@pytest_asyncio.fixture()
async def client(stores: SimpleNamespace, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app(stores, tmp_path, enabled=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _session(stores: SimpleNamespace, agent_id: str) -> str:
    stores.agents.create(agent_id, f"agent-{agent_id[0]}", f"{agent_id}/{_BUNDLE_HASH}")
    return stores.conversations.create_conversation(agent_id=agent_id).id


def _body(**overrides: Any) -> dict[str, Any]:
    body = {
        "kind": "followup",
        "title": "Ramp up the flag",
        "body_md": "The PR merged yesterday.",
        "links": [{"type": "pr", "ref": "https://github.com/acme/app/pull/7/files"}],
        "topic": "Rampup Flag",
    }
    body.update(overrides)
    return body


async def test_post_creates_an_item_from_an_inbox_agent_session(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session_id = _session(stores, _INBOX_AGENT)

    resp = await client.post(
        "/v1/inbox/items", json=_body(), headers={ORCHESTRATION_SOURCE_HEADER: session_id}
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["duplicate"] is False
    item = payload["item"]
    assert item["source_session_id"] == session_id
    assert item["agent_id"] == _INBOX_AGENT
    assert item["prompt_version"] == _BUNDLE_HASH
    assert item["primary_link"] == "github.com/acme/app/pull/7"
    assert item["topic"] == "rampup-flag"
    assert item["state"] == "unread"


async def test_repeat_post_returns_the_existing_item(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session_id = _session(stores, _INBOX_AGENT)
    headers = {ORCHESTRATION_SOURCE_HEADER: session_id}
    first = (await client.post("/v1/inbox/items", json=_body(), headers=headers)).json()

    again = await client.post(
        "/v1/inbox/items",
        json=_body(links=[{"type": "pr", "ref": "github.com/ACME/app/pull/7"}], title="Other"),
        headers=headers,
    )

    assert again.json() == {"item": first["item"], "duplicate": True}


@pytest.mark.parametrize(
    ("agent_id", "headers_for", "status"),
    [
        (_INBOX_AGENT, lambda session_id: {}, 403),
        (_PLAIN_AGENT, lambda session_id: {ORCHESTRATION_SOURCE_HEADER: session_id}, 403),
        (_INBOX_AGENT, lambda session_id: {ORCHESTRATION_SOURCE_HEADER: "9" * 32}, 404),
    ],
    ids=["no-source-session", "agent-without-inbox-group", "unknown-session"],
)
async def test_post_requires_an_inbox_agent_session(
    client: httpx.AsyncClient,
    stores: SimpleNamespace,
    agent_id: str,
    headers_for: Any,
    status: int,
) -> None:
    session_id = _session(stores, agent_id)

    resp = await client.post("/v1/inbox/items", json=_body(), headers=headers_for(session_id))

    assert resp.status_code == status, resp.text


async def test_post_is_capped_per_session(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    headers = {ORCHESTRATION_SOURCE_HEADER: _session(stores, _INBOX_AGENT)}
    for n in range(MAX_ITEMS_PER_SESSION):
        resp = await client.post("/v1/inbox/items", json=_body(topic=f"t{n}"), headers=headers)
        assert resp.status_code == 200

    resp = await client.post("/v1/inbox/items", json=_body(topic="one-more"), headers=headers)

    assert resp.status_code == 400


async def test_list_and_update(client: httpx.AsyncClient, stores: SimpleNamespace) -> None:
    headers = {ORCHESTRATION_SOURCE_HEADER: _session(stores, _INBOX_AGENT)}
    item = (await client.post("/v1/inbox/items", json=_body(), headers=headers)).json()["item"]

    listed = (await client.get("/v1/inbox/items")).json()
    assert [entry["id"] for entry in listed["data"]] == [item["id"]]
    assert listed["unread_count"] == 1
    assert listed["next_cursor"] is None

    updated = await client.patch(
        f"/v1/inbox/items/{item['id']}", json={"state": "dismissed", "feedback": "not_useful"}
    )
    assert updated.status_code == 200
    assert (updated.json()["state"], updated.json()["feedback"]) == ("dismissed", "not_useful")
    unread = (await client.get("/v1/inbox/items", params={"state": "unread"})).json()
    assert (unread["data"], unread["unread_count"]) == ([], 0)

    assert (await client.patch(f"/v1/inbox/items/{'0' * 32}", json={})).status_code == 404
    assert (await client.patch("/v1/inbox/items/not-an-id", json={})).status_code == 404
    assert (await client.get("/v1/inbox/items", params={"after": "x"})).status_code == 400


async def test_routes_are_off_without_the_feature(stores: SimpleNamespace, tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=_app(stores, tmp_path, enabled=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/v1/inbox/items")).status_code == 404
