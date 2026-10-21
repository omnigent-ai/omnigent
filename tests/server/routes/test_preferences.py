"""Request authentication and validation at the shared-preference HTTP boundary."""

from uuid import uuid4

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore


@pytest.mark.parametrize("positional_auth", [False, True])
async def test_authenticated_preference_operations(
    runtime_init, db_uri, tmp_path, monkeypatch, positional_auth
):
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="test-authority")
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    stores = [
        SqlAlchemyAgentStore(db_uri),
        SqlAlchemyFileStore(db_uri),
        SqlAlchemyConversationStore(db_uri),
        artifacts,
        AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
    ]
    provider = UnifiedAuthProvider(source="header")
    # Existing embedders pass auth after six optional stores/tunnel arguments.
    app = (
        create_app(*stores, *([None] * 6), provider, preference_store=store)
        if positional_auth
        else create_app(*stores, auth_provider=provider, preference_store=store)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/v1/preferences/shared"
        assert (await client.get(path)).status_code == 401
        alice = {"X-Forwarded-Email": "alice@example.test"}
        bob = {"X-Forwarded-Email": "bob@example.test"}
        initial = (await client.get(path, headers=alice)).json()
        operation = {
            "protocol_version": 1,
            "identity": initial["identity"],
            "operation_id": str(uuid4()),
            "installation_id": "laptop",
            "key": {"kind": "harness", "name": "default"},
            "expected_version": 0,
            "value": "pi-native",
        }
        assert (await client.post(path + "/operations", json=operation)).status_code == 401
        assert (
            await client.post(path + "/operations", json=operation, headers=bob)
        ).status_code == 403
        invalid = {**operation, "value": False}
        assert (
            await client.post(path + "/operations", json=invalid, headers=alice)
        ).status_code == 422
        response = await client.post(path + "/operations", json=operation, headers=alice)
        assert response.status_code == 200, response.text
        assert response.json()["entry"]["value"] == "pi-native"
        assert (await client.get(path, headers=bob)).json()["entries"] == []
        assert (
            await client.post(path + "/operations", json=operation, headers=alice)
        ).json() == response.json()
        assert (
            await client.post(
                path + "/operations", json={**operation, "value": "claude-native"}, headers=alice
            )
        ).status_code == 409
        conflict = {**operation, "operation_id": str(uuid4()), "value": "codex-native"}
        assert (await client.post(path + "/operations", json=conflict, headers=alice)).json()[
            "status"
        ] == "conflict"
        first = (await client.get(path, params={"limit": 1}, headers=alice)).json()
        assert first["next_cursor"] and len(first["conflicts"]) == 1
        last = (
            await client.get(
                path, params={"limit": 1, "cursor": first["next_cursor"]}, headers=alice
            )
        ).json()
        assert last["next_cursor"] is None and len(last["entries"]) == 1
        assert first["revision"] == last["revision"]
        for params, status in [
            ({"limit": 0}, 422),
            ({"limit": 101}, 422),
            ({"cursor": "bad"}, 400),
        ]:
            assert (await client.get(path, params=params, headers=alice)).status_code == status
        newer = {
            **operation,
            "operation_id": str(uuid4()),
            "expected_version": 1,
            "value": "claude-native",
        }
        assert (
            await client.post(path + "/operations", json=newer, headers=alice)
        ).status_code == 200
        assert (
            await client.get(path, params={"cursor": first["next_cursor"]}, headers=alice)
        ).status_code == 409


async def test_preferences_not_mounted_by_default(client):
    assert (await client.get("/v1/preferences/shared")).status_code == 404
