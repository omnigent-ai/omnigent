"""Request authentication and validation at the shared-preference HTTP boundary."""

from uuid import uuid4

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.preference_requests import PreferencePolicy
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore


@pytest.mark.parametrize("auth_mode", ["keyword", "positional", "disabled"])
async def test_authenticated_preference_operations(db_uri, tmp_path, monkeypatch, auth_mode):
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
    provider = None if auth_mode == "disabled" else UnifiedAuthProvider(source="header")
    enabled = True
    policy = PreferencePolicy(enabled=lambda: enabled)
    # Exercise positional auth while keeping preference_store keyword-only.
    app = (
        create_app(
            *stores, *([None] * 6), provider, preference_store=store, preference_policy=policy
        )
        if auth_mode == "positional"
        else create_app(
            *stores, auth_provider=provider, preference_store=store, preference_policy=policy
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        profile_path = "/v1/preferences/configuration"
        if auth_mode != "disabled":
            assert (await client.get(profile_path)).status_code == 401
        profile_headers = (
            {} if auth_mode == "disabled" else {"X-Forwarded-Email": "alice@example.test"}
        )
        profile = (await client.get(profile_path, headers=profile_headers)).json()
        profile_save = {
            "identity": profile["identity"],
            "expected_revision": 0,
            "configuration": {"model": "imported"},
        }
        assert (
            await client.put(profile_path, headers=profile_headers, json=profile_save)
        ).status_code == 200
        assert (await client.get(profile_path, headers=profile_headers)).json()["configuration"][
            "model"
        ] == "imported"
        assert (
            await client.put(
                profile_path,
                headers=profile_headers,
                json={**profile_save, "configuration": {"providers": {"token": "never-store"}}},
            )
        ).status_code == 422
        if auth_mode != "disabled":
            assert (
                await client.put(
                    profile_path,
                    headers={"X-Forwarded-Email": "bob@example.test"},
                    json=profile_save,
                )
            ).status_code == 403
        enabled = False
        assert (await client.get(profile_path, headers=profile_headers)).status_code == 404
        assert (
            await client.put(profile_path, headers=profile_headers, json=profile_save)
        ).status_code == 404
        enabled = True
        path = "/v1/preferences/shared"
        if auth_mode != "disabled":
            assert (await client.get(path)).status_code == 401
            if auth_mode == "positional":
                return
        alice = {} if auth_mode == "disabled" else {"X-Forwarded-Email": "alice@example.test"}
        bob = {"X-Forwarded-Email": "bob@example.test"}
        response = await client.get(path, headers=alice)
        assert response.status_code == 200, response.text
        initial = response.json()
        operation = {
            "protocol_version": 1,
            "identity": initial["identity"],
            "operation_id": str(uuid4()),
            "installation_id": "laptop",
            "key": {"kind": "harness", "name": "default"},
            "expected_version": 0,
            "value": "pi-native",
        }
        if auth_mode == "disabled":
            assert initial["identity"]["user_id"] == "local"
            saved = await client.post(path + "/operations", json=operation)
            assert saved.status_code == 200, saved.text
            assert (await client.get(path)).json()["entries"] == [saved.json()["entry"]]
            assert store.read(user_id=None).entries[0].value == "pi-native"
            foreign = {**operation, "identity": {**initial["identity"], "user_id": "alice"}}
            assert (await client.post(path + "/operations", json=foreign)).status_code == 403
            return
        assert (await client.post(path + "/operations", json=operation)).status_code == 401
        overlong = {"X-Forwarded-Email": "a" * 123 + "@example.test"}
        assert (await client.get(path, headers=overlong)).status_code == 400
        assert (
            await client.post(path + "/operations", json=operation, headers=overlong)
        ).status_code == 400
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
            ({"cursor": "x" * 4097}, 422),
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
        monkeypatch.setattr(store, "_authority", "other-authority")
        assert (await client.get(path, headers=alice)).status_code == 409
        foreign_authority = {
            **operation,
            "identity": {**initial["identity"], "authority_id": "other-authority"},
        }
        assert (
            await client.post(path + "/operations", json=foreign_authority, headers=alice)
        ).status_code == 409


async def test_preferences_not_mounted_by_default(client):
    assert (await client.get("/v1/preferences/configuration")).status_code == 404
    assert (await client.get("/v1/preferences/shared")).status_code == 404


async def test_configuration_body_limit_before_parsing(db_uri):
    from fastapi import FastAPI

    from omnigent.server.routes.preferences import create_preferences_router

    app = FastAPI()
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="bounded-test")
    app.include_router(create_preferences_router(store))

    async def chunks():
        yield b" " * 9000
        yield b" " * 9000

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put("/preferences/configuration", content=chunks())
        assert response.status_code == 413
        assert store.read_configuration(user_id=None).configuration is None
