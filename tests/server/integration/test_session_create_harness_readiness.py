"""Session creation refuses unavailable harnesses before persisting a child."""

import json

import httpx
import pytest
from fastapi import FastAPI

from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize(
    "readiness", [False, "binary-missing", "needs-auth", "version-too-low", "absent"]
)
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


@pytest.mark.parametrize("readiness", [None, {}, {"jcode": True}])
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


async def test_top_level_create_rejects_unavailable_harness(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    from omnigent.stores.host_store import HostStore

    agent = await create_test_agent(client)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": False},
    )
    app.state.host_store = hosts
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "workspace": "/tmp/workspace",
            "harness_override": "jcode",
        },
    )
    assert response.status_code == 412, response.text
    assert response.json()["error"]["code"] == "harness_not_configured"


async def test_agent_id_child_uses_parent_host_even_if_request_names_another(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

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


@pytest.mark.parametrize("reason,allowed", [("needs-auth", True), ("binary-missing", False)])
async def test_inference_binding_only_overrides_auth_readiness(
    db_uri: str,
    reason: str,
    allowed: bool,
) -> None:
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.routes._session_harness_readiness import validate_create_harness_readiness
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "test-host",
        "local",
        configured_harnesses={"jcode": reason},
    )
    snapshot = {
        "harness": "jcode",
        "runtime_config": {
            "providers": {
                "test-provider": {
                    "kind": "gateway",
                    "openai": {
                        "base_url": "https://gateway.example/v1",
                        "api_key_ref": "env:TEST_KEY",
                    },
                }
            },
            "inference": {
                "harnesses": {
                    "jcode": {"provider": "test-provider", "default_model": "test-model"},
                }
            },
        },
    }

    async def validate() -> None:
        await validate_create_harness_readiness(
            harness="jcode",
            host_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            parent_session_id=None,
            inherited_runner_id=None,
            user_id=None,
            conversation_store=SqlAlchemyConversationStore(db_uri),
            host_store=hosts,
            inference_snapshot=snapshot,
        )

    if allowed:
        await validate()
    else:
        with pytest.raises(OmnigentError) as exc:
            await validate()
        assert exc.value.code == ErrorCode.HARNESS_NOT_CONFIGURED
