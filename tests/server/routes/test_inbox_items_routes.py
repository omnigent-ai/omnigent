"""Tests for the ``/v1/inbox/items`` routes."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes.inbox_items import MAX_ITEMS_PER_SESSION
from omnigent.spec.types import ToolGroupsConfig
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_INBOX_AGENT = "1" * 32
_PLAIN_AGENT = "2" * 32
_BUNDLE_HASH = "c" * 64
_TOKEN = "runner-binding-token"


class _AgentCacheStub:
    """Loads a spec whose ``user_inbox`` group is on only for the Inbox agent."""

    def load(self, agent_id: str, bundle_location: str, **_: Any) -> SimpleNamespace:
        groups = ToolGroupsConfig(user_inbox=agent_id == _INBOX_AGENT)
        return SimpleNamespace(spec=SimpleNamespace(tool_groups=groups))


@pytest.fixture()
def stores(db_uri: str) -> SimpleNamespace:
    agents = SqlAlchemyAgentStore(db_uri)
    for agent_id in (_INBOX_AGENT, _PLAIN_AGENT):
        agents.create(agent_id, f"agent-{agent_id[0]}", f"{agent_id}/{_BUNDLE_HASH}")
    return SimpleNamespace(
        agents=agents,
        conversations=SqlAlchemyConversationStore(db_uri),
        inbox=SqlAlchemyInboxItemStore(db_uri),
        permissions=SqlAlchemyPermissionStore(db_uri),
    )


def _app(
    stores: SimpleNamespace,
    tmp_path: Path,
    *,
    enabled: bool = True,
    auth: bool = False,
    **extra: Any,
) -> FastAPI:
    if auth:
        extra["auth_provider"] = UnifiedAuthProvider(source="header", local_single_user=False)
        extra["permission_store"] = stores.permissions
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
        **extra,
    )


@pytest_asyncio.fixture()
async def client(stores: SimpleNamespace, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app(stores, tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _session(stores: SimpleNamespace, agent_id: str = _INBOX_AGENT, *, bound: bool = True) -> str:
    runner_id = token_bound_runner_id(_TOKEN) if bound else None
    return stores.conversations.create_conversation(agent_id=agent_id, runner_id=runner_id).id


def _from(session_id: str, token: str = _TOKEN) -> dict[str, str]:
    return {ORCHESTRATION_SOURCE_HEADER: session_id, RUNNER_TUNNEL_TOKEN_HEADER: token}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "followup",
        "title": "Ramp up the flag",
        "body_md": "The PR merged yesterday.",
        "links": [{"type": "pr", "ref": "https://github.com/acme/app/pull/7/files"}],
        "actions": [{"label": "Ramp it up", "prompt": "Ramp up the flag for acme/app#7"}],
        "topic": "Rampup Flag",
    }
    body.update(overrides)
    return body


async def test_post_creates_an_item(client: httpx.AsyncClient, stores: SimpleNamespace) -> None:
    session_id = _session(stores)

    resp = await client.post("/v1/inbox/items", json=_body(), headers=_from(session_id))

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["duplicate"] is False
    item = payload["item"]
    assert (item["source_session_id"], item["agent_id"]) == (session_id, _INBOX_AGENT)
    assert item["prompt_version"] == _BUNDLE_HASH
    assert (item["primary_link"], item["topic"]) == ("github.com/acme/app/pull/7", "rampup-flag")
    assert item["actions"] == _body()["actions"]
    assert (item["state"], item["resolution"]) == ("unread", "none")


async def test_post_stores_a_chart(client: httpx.AsyncClient, stores: SimpleNamespace) -> None:
    chart = {
        "version": 1,
        "type": "bar",
        "title": "Failures",
        "x": ["a", "b"],
        "series": [{"name": "n", "values": [1, 2]}],
    }

    ok = await client.post(
        "/v1/inbox/items", json=_body(chart=chart), headers=_from(_session(stores))
    )
    bad = await client.post(
        "/v1/inbox/items",
        json=_body(topic="other", chart={**chart, "x": ["a"]}),
        headers=_from(_session(stores)),
    )

    assert ok.json()["item"]["chart"] == {
        **chart,
        "markers": [],
        "series": [{"name": "n", "values": [1.0, 2.0], "emphasis": False}],
    }
    assert bad.status_code == 422
    assert "one value per x label" in bad.text


async def test_repeat_post_returns_the_existing_item(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    headers = _from(_session(stores))
    first = (await client.post("/v1/inbox/items", json=_body(), headers=headers)).json()

    again = await client.post(
        "/v1/inbox/items",
        json=_body(links=[{"type": "pr", "ref": "github.com/ACME/app/pull/7"}], title="Other"),
        headers=headers,
    )

    assert again.json() == {"item": first["item"], "duplicate": True}


async def test_an_expired_item_is_replaced(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    headers = _from(_session(stores))
    first = (
        await client.post("/v1/inbox/items", json=_body(expires_at=1), headers=headers)
    ).json()

    again = (await client.post("/v1/inbox/items", json=_body(), headers=headers)).json()

    assert again["duplicate"] is False
    assert again["item"]["id"] != first["item"]["id"]


@pytest.mark.parametrize(
    ("agent_id", "bound", "headers_for"),
    [
        (_INBOX_AGENT, True, lambda session_id: {RUNNER_TUNNEL_TOKEN_HEADER: _TOKEN}),
        (_PLAIN_AGENT, True, _from),
        (_INBOX_AGENT, True, lambda session_id: _from("9" * 32)),
        (_INBOX_AGENT, False, _from),
        (_INBOX_AGENT, True, lambda session_id: _from(session_id, token="another-runner")),
    ],
    ids=[
        "no-source-session",
        "agent-without-inbox-group",
        "unknown-session",
        "unbound",
        "wrong-runner",
    ],
)
async def test_post_requires_a_bound_inbox_agent_session(
    client: httpx.AsyncClient,
    stores: SimpleNamespace,
    agent_id: str,
    bound: bool,
    headers_for: Any,
) -> None:
    session_id = _session(stores, agent_id, bound=bound)

    resp = await client.post("/v1/inbox/items", json=_body(), headers=headers_for(session_id))

    assert resp.status_code == 403, resp.text


async def test_post_cap_holds_under_concurrency_and_id_spellings(
    client: httpx.AsyncClient, stores: SimpleNamespace
) -> None:
    session_id = _session(stores)
    for n in range(MAX_ITEMS_PER_SESSION - 1):
        resp = await client.post(
            "/v1/inbox/items", json=_body(topic=f"t{n}"), headers=_from(session_id)
        )
        assert resp.status_code == 200, resp.text

    racing = await asyncio.gather(
        *(
            client.post("/v1/inbox/items", json=_body(topic=topic), headers=_from(session_id))
            for topic in ("race-a", "race-b")
        )
    )
    dashed = str(uuid.UUID(session_id))
    alias = await client.post("/v1/inbox/items", json=_body(topic="alias"), headers=_from(dashed))

    assert sorted(resp.status_code for resp in racing) == [200, 400]
    assert alias.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"links": [{"type": "url", "ref": "https://["}]},
        {"body_md": "".join(chr(0x4E00 + (n * 7919) % 20000) for n in range(8000))},
    ],
    ids=["unparseable-link", "body-too-large-to-store"],
)
async def test_post_rejects_items_it_cannot_store(
    client: httpx.AsyncClient, stores: SimpleNamespace, overrides: dict[str, Any]
) -> None:
    resp = await client.post(
        "/v1/inbox/items", json=_body(**overrides), headers=_from(_session(stores))
    )

    assert resp.status_code == 400, resp.text


async def test_list_and_resolve(client: httpx.AsyncClient, stores: SimpleNamespace) -> None:
    headers = _from(_session(stores))
    item = (await client.post("/v1/inbox/items", json=_body(), headers=headers)).json()["item"]
    url = f"/v1/inbox/items/{item['id']}"

    listed = (await client.get("/v1/inbox/items")).json()
    assert [entry["id"] for entry in listed["data"]] == [item["id"]]
    assert (listed["unread_count"], listed["unread_count_capped"]) == (1, False)
    assert listed["next_cursor"] is None

    taken = await client.patch(url, json={"resolution": "action_taken", "resolution_action": 0})
    assert taken.status_code == 200
    assert (taken.json()["state"], taken.json()["resolution_action"]) == ("done", 0)
    reopened = (await client.patch(url, json={"resolution": "none"})).json()
    assert (reopened["state"], reopened["resolution"]) == ("read", "none")
    dismissed = (await client.patch(url, json={"resolution": "not_now"})).json()
    assert (dismissed["state"], dismissed["resolution"]) == ("dismissed", "not_now")
    unread = (await client.get("/v1/inbox/items", params={"state": "unread"})).json()
    assert (unread["data"], unread["unread_count"]) == ([], 0)

    assert (
        await client.patch(url, json={"resolution": "action_taken", "resolution_action": 2})
    ).status_code == 400
    assert (
        await client.patch(url, json={"state": "read", "resolution": "useful"})
    ).status_code == 422
    assert (await client.patch(f"/v1/inbox/items/{'0' * 32}", json={})).status_code == 404
    assert (await client.patch("/v1/inbox/items/not-an-id", json={})).status_code == 404
    assert (await client.get("/v1/inbox/items", params={"after": "x"})).status_code == 400


async def test_authenticated_owners_see_only_their_items(
    stores: SimpleNamespace, tmp_path: Path
) -> None:
    session_id = _session(stores)
    stores.permissions.grant("alice@example.com", session_id, LEVEL_OWNER)
    transport = httpx.ASGITransport(app=_app(stores, tmp_path, auth=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        alice = {"X-Forwarded-Email": "alice@example.com"}
        bob = {"X-Forwarded-Email": "bob@example.com"}
        posted = await c.post(
            "/v1/inbox/items", json=_body(), headers={**alice, **_from(session_id)}
        )
        assert posted.status_code == 200, posted.text
        item_id = posted.json()["item"]["id"]

        stolen = await c.post(
            "/v1/inbox/items", json=_body(topic="x"), headers={**bob, **_from(session_id)}
        )
        assert stolen.status_code == 403
        assert (await c.get("/v1/inbox/items", headers=bob)).json()["data"] == []
        assert (
            await c.patch(f"/v1/inbox/items/{item_id}", json={"state": "read"}, headers=bob)
        ).status_code == 404
        assert len((await c.get("/v1/inbox/items", headers=alice)).json()["data"]) == 1


async def test_routes_are_off_without_the_feature(stores: SimpleNamespace, tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=_app(stores, tmp_path, enabled=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/v1/inbox/items")).status_code == 404
        assert (await c.get("/v1/info")).json()["proactive_agent_enabled"] is False


async def test_a_per_request_gate_turns_the_routes_on_and_off(
    stores: SimpleNamespace, tmp_path: Path
) -> None:
    gate = {"on": False}
    app = _app(stores, tmp_path, enabled=False, proactive_agent=lambda: gate["on"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/v1/inbox/items")).status_code == 404
        gate["on"] = True
        assert (await c.get("/v1/inbox/items")).status_code == 200
        assert (await c.get("/v1/info")).json()["proactive_agent_enabled"] is True
