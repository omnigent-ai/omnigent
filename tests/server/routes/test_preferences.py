"""Request authentication and validation at the shared-preference HTTP boundary."""

from uuid import uuid4

import httpx

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore


async def test_authenticated_preference_operations(runtime_init, db_uri, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="test-authority")
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        preference_store=store,
        auth_provider=UnifiedAuthProvider(source="header"),
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


async def test_preferences_not_mounted_by_default(client):
    assert (await client.get("/v1/preferences/shared")).status_code == 404
