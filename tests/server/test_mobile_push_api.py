import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_mobile_push_config import credentials as credentials


@pytest.fixture
def push_app_factory(db_uri, tmp_path, credentials, monkeypatch):
    from omnigent.server.app import create_app

    path, _ = credentials
    monkeypatch.setenv("OMNIGENT_FCM_CREDENTIALS_FILE", str(path))
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))

    def build(*, active=True, auth="header", configured=True):
        if configured:
            monkeypatch.setenv("OMNIGENT_FCM_CREDENTIALS_FILE", str(path))
        else:
            monkeypatch.delenv("OMNIGENT_FCM_CREDENTIALS_FILE", raising=False)
        return create_app(
            agent_store=SqlAlchemyAgentStore(db_uri),
            file_store=SqlAlchemyFileStore(db_uri),
            conversation_store=SqlAlchemyConversationStore(db_uri),
            artifact_store=artifacts,
            agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
            permission_store=SqlAlchemyPermissionStore(db_uri) if auth else None,
            auth_provider=None
            if auth is None
            else UnifiedAuthProvider(source="header", local_single_user=auth == "local"),
            feature_flags=resolve_feature_flags(
                {"OMNIGENT_FEATURES": "mobile_push" if active else ""}
            ),
            server_config={},
        )

    return build


@pytest.mark.parametrize(
    "flag,configured", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_info_reports_config_state_and_dormant_routes(push_app_factory, flag, configured):
    app = push_app_factory(active=flag, configured=configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/info")
        assert response.status_code == 200
        enabled = flag and configured
        assert response.json()["push"] == {
            "fcm": {"enabled": enabled, "project_id": "push-project" if enabled else None},
            "preview": False,
        }
        registration = await client.put(
            "/v1/mobile-push/devices/phone",
            headers={"X-Forwarded-Email": "alice"},
            json={
                "platform": "android",
                "fcm_token": "token",
                "firebase_project_id": "push-project",
            },
        )
        assert registration.status_code == (204 if enabled else 404)


@pytest.mark.parametrize(
    "auth,headers,status", [(None, {}, 401), ("header", {}, 401), ("local", {}, 403)]
)
async def test_device_routes_reject_missing_identity_and_local_mode(
    push_app_factory, auth, headers, status
):
    app = push_app_factory(auth=auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v1/mobile-push/devices/phone",
            headers=headers,
            json={"platform": "ios", "fcm_token": "token", "firebase_project_id": "push-project"},
        )
        assert response.status_code == status


@pytest.mark.parametrize("flag,configured", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize(
    "method,identifier,content",
    [
        ("PUT", "phone", b""),
        ("PUT", "phone", b"{"),
        ("PUT", "phone", b'{"platform":"other"}'),
        ("PUT", "bad.id", b"{}"),
        ("DELETE", "bad.id", None),
        ("GET", "phone", None),
        ("POST", "phone", b"{"),
    ],
)
async def test_inactive_routes_gate_before_validation(
    push_app_factory, flag, configured, method, identifier, content
):
    app = push_app_factory(active=flag, configured=configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.request(
            method,
            f"/v1/mobile-push/devices/{identifier}",
            content=content,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 404
    active = push_app_factory()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(active), base_url="http://test"
    ) as client:
        validated = await client.request(
            method,
            f"/v1/mobile-push/devices/{identifier}",
            content=content,
            headers={"Content-Type": "application/json"},
        )
    assert validated.status_code == (422 if method in {"PUT", "DELETE"} else 405)


async def test_device_api_forged_owner_guessed_installation_delete_and_project(
    push_app_factory, db_uri
):
    app = push_app_factory()
    body = {
        "platform": "android",
        "fcm_token": "alice-token",
        "firebase_project_id": "push-project",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v1/mobile-push/devices/phone",
            headers={"X-Forwarded-Email": "alice"},
            json=body | {"owner": "bob", "app_version": "next"},
        )
        assert response.status_code == 204
        from omnigent.server.mobile_push_store import MobilePushStore

        stored = MobilePushStore(db_uri)
        assert len(stored.devices_for_user("alice")) == 1
        assert stored.devices_for_user("bob") == []
        response = await client.put(
            "/v1/mobile-push/devices/phone",
            headers={"X-Forwarded-Email": "alice"},
            json=body | {"firebase_project_id": "wrong-project"},
        )
        assert response.status_code == 409
        assert (
            await client.put(
                "/v1/mobile-push/devices/phone", headers={"X-Forwarded-Email": "alice"}, json=body
            )
        ).status_code == 204
        assert (
            await client.put(
                "/v1/mobile-push/devices/phone",
                headers={"X-Forwarded-Email": "bob"},
                json=body | {"fcm_token": "bob-token"},
            )
        ).status_code == 409
        assert (
            await client.delete(
                "/v1/mobile-push/devices/phone", headers={"X-Forwarded-Email": "bob"}
            )
        ).status_code == 204
        assert len(app.state.mobile_push_store.devices_for_user("alice")) == 1
        assert (
            await client.put(
                "/v1/mobile-push/devices/phone", headers={"X-Forwarded-Email": "bob"}, json=body
            )
        ).status_code == 204
        assert app.state.mobile_push_store.devices_for_user("alice") == []
        assert (
            await client.delete(
                "/v1/mobile-push/devices/phone", headers={"X-Forwarded-Email": "bob"}
            )
        ).status_code == 204
        assert app.state.mobile_push_store.devices_for_user("bob") == []


async def test_device_api_workspace_isolation(db_uri):
    from omnigent.db.db_models import workspace_scope
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_store import MobilePushStore
    from omnigent.server.routes.mobile_push import create_mobile_push_router

    store = MobilePushStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_mobile_push_router(
            store,
            FcmConfig("push-project", "", ""),
            UnifiedAuthProvider(source="header", local_single_user=False),
        )
    )
    body = {"platform": "ios", "fcm_token": "same-token", "firebase_project_id": "push-project"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for workspace in (11, 22):
            with workspace_scope(workspace):
                assert (
                    await client.put(
                        "/v1/mobile-push/devices/phone",
                        headers={"X-Forwarded-Email": "same-user"},
                        json=body,
                    )
                ).status_code == 204
        with workspace_scope(11):
            assert (
                await client.delete(
                    "/v1/mobile-push/devices/phone", headers={"X-Forwarded-Email": "same-user"}
                )
            ).status_code == 204
            assert store.devices_for_user("same-user") == []
        with workspace_scope(22):
            assert len(store.devices_for_user("same-user")) == 1
