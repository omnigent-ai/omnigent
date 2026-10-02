"""Session creation refuses unavailable harnesses before persisting a child."""

import json

import httpx
import pytest
from fastapi import FastAPI

from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("readiness", [False, "binary-missing", "version-too-low", "absent"])
async def test_child_create_rejects_unavailable_harness(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    bundle: bool,
    readiness: bool | str,
) -> None:
    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    from omnigent.harness_availability import HarnessAvailability
    from omnigent.stores.host_store import HostStore

    report: dict[str, HarnessAvailability] = {"claude-sdk": True}
    if readiness != "absent":
        report["jcode"] = readiness
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "test-host", "local", configured_harnesses=report
    )
    app.state.host_store = host_store
    before = store.list_conversations(limit=100).data
    if bundle:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({"parent_session_id": parent["id"]})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(
                        "jcode-child",
                        executor={"type": "omnigent", "config": {"harness": "jcode"}},
                    ),
                    "application/gzip",
                )
            },
        )
    else:
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "parent_session_id": parent["id"],
                "harness_override": "jcode",
            },
        )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "harness_not_configured"
    assert "jcode" in response.json()["error"]["message"]
    assert len(store.list_conversations(limit=100).data) == len(before)


@pytest.mark.parametrize("readiness", [None, {}, {"jcode": True}, {"jcode": "needs-auth"}])
async def test_child_create_preserves_ready_and_unknown_hosts(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    readiness: dict | None,
) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    parent = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "test-host", "local", configured_harnesses=readiness
    )
    app.state.host_store = hosts
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent["id"],
            "harness_override": "jcode",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["harness"] == "jcode"


@pytest.mark.parametrize("bundle", [False, True])
async def test_top_level_create_rejects_unavailable_harness(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    bundle: bool,
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.server.routes import _session_create_validation
    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(
        orchestration, "_validate_session_workspace", AsyncMock(return_value="/tmp/workspace")
    )

    monkeypatch.setattr(
        _session_create_validation,
        "validate_uploaded_bundle_host_workspace",
        AsyncMock(return_value="/tmp/workspace"),
    )
    agent = await create_test_agent(client)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": False},
    )
    app.state.host_store = hosts
    metadata = {
        "host_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "workspace": "/tmp/workspace",
    }
    if bundle:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(
                        "jcode-top", executor={"type": "omnigent", "config": {"harness": "jcode"}}
                    ),
                    "application/gzip",
                )
            },
        )
    else:
        response = await client.post(
            "/v1/sessions",
            json={**metadata, "agent_id": agent["id"], "harness_override": "jcode"},
        )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "harness_not_configured"


async def test_agent_id_child_uses_parent_host_even_if_request_names_another(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(
        orchestration, "_validate_session_workspace", AsyncMock(return_value="/tmp/workspace")
    )

    parent_agent = await create_test_agent(client)
    jcode = await create_test_agent(
        client,
        name="jcode-worker",
        executor={
            "type": "omnigent",
            "config": {"harness": "jcode"},
        },
    )
    parent = (await client.post("/v1/sessions", json={"agent_id": parent_agent["id"]})).json()
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    store.set_runner_id(parent["id"], "runner_test")
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "parent-host",
        "local",
        configured_harnesses={"jcode": False},
    )
    hosts.upsert_on_connect(
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "other-host",
        "local",
        configured_harnesses={"jcode": True},
    )
    app.state.host_store = hosts
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": jcode["id"],
            "parent_session_id": parent["id"],
            "host_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "workspace": "/tmp/workspace",
        },
    )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "harness_not_configured"


async def test_loaded_parent_does_not_require_another_session_read(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes._session_harness_readiness import validate_create_harness_readiness
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    parent_id = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(parent_id, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", workspace="/tmp/workspace")
    parent = store.get_conversation(parent_id)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": True},
    )

    def unexpected_read(session_id: str) -> None:
        raise AssertionError("Already-loaded parent must be reused")

    monkeypatch.setattr(store, "get_conversation", unexpected_read)
    await validate_create_harness_readiness(
        harness="jcode",
        host_id=None,
        parent_session_id=parent_id,
        inherited_runner_id="runner_test",
        user_id=None,
        conversation_store=store,
        host_store=hosts,
        parent=parent,
    )


@pytest.mark.parametrize("depth", [1, 16, 17, 32])
async def test_nested_parent_readiness_has_bounded_reads(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    depth: int,
) -> None:
    from unittest.mock import Mock

    from omnigent.errors import OmnigentError
    from omnigent.server.routes._session_harness_readiness import validate_create_harness_readiness
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(
        agent_id=agent["id"],
        host_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        workspace="/tmp/workspace",
    )
    for _ in range(depth):
        parent = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=parent.id,
            agent_id=agent["id"],
            runner_id="runner_test",
        )
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": False},
    )
    read = Mock(wraps=store.get_conversation)
    monkeypatch.setattr(store, "get_conversation", read)
    validation = validate_create_harness_readiness(
        harness="jcode",
        host_id=None,
        parent_session_id=parent.id,
        inherited_runner_id="runner_test",
        user_id=None,
        conversation_store=store,
        host_store=hosts,
        parent=parent,
    )
    if depth <= 16:
        with pytest.raises(OmnigentError, match="not configured"):
            await validation
    else:
        await validation
    assert read.call_count == min(depth, 16)
