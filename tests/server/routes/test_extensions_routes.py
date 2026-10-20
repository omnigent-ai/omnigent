"""Tests for the installed-extension catalog and diagnostics routes."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import omnigent.server.app as app_module
from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    ExtensionPluginState,
    PageContribution,
    PrimaryNavigationContribution,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_ADMIN = "admin@extensions.test"
_USER = "user@extensions.test"


def _manifest(extension_id: str = "acme.review") -> ExtensionManifest:
    page_id = f"{extension_id}.dashboard"
    return ExtensionManifest(
        id=extension_id,
        display_name="Acme Review",
        distribution="omnigent-acme-review",
        version="1.2.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset(
            {
                ExtensionPermission.NAVIGATION,
                ExtensionPermission.SESSIONS_READ,
                ExtensionPermission.STORAGE_USER,
            }
        ),
        pages=(
            PageContribution(
                id=page_id,
                title="Review dashboard",
                route="dashboard",
                view="review-dashboard",
            ),
        ),
        primary_navigation=(
            PrimaryNavigationContribution(
                id=f"{extension_id}.primary-nav",
                label="Code Review",
                page=page_id,
                icon="search",
                order=350,
            ),
        ),
    )


def _state() -> ExtensionPluginState:
    return ExtensionPluginState(
        manifests=(_manifest(),),
        load_errors={"broken:entry": "extension import failed"},
    )


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    extension_state: ExtensionPluginState | None = None,
    permission_store: SqlAlchemyPermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
    server_config: dict | None = None,
) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=permission_store,
        auth_provider=auth_provider,
        extension_state=extension_state,
        server_config=server_config,
    )


def _client(app: FastAPI, email: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": email} if email else {},
    )


@pytest.fixture
def team_state(monkeypatch: pytest.MonkeyPatch) -> ExtensionPluginState:
    source = Path(__file__).parents[3] / "examples/extensions/incident-response/src"
    monkeypatch.syspath_prepend(str(source))
    manifest = importlib.import_module("omnigent_incident_response.plugin").get_manifest()
    return ExtensionPluginState(
        manifests=(manifest,), asset_packages={manifest.id: "omnigent_incident_response"}
    )


def _team_config(enabled: bool = True) -> dict:
    return {
        "extensions": {
            "acme.incident-response": {
                "enabled": enabled,
                "version": "==0.1.0",
                "permissions": ["events.subscribe"],
            }
        }
    }


async def test_team_pack_requires_activation_and_has_admin_diagnostics(
    db_uri, tmp_path, team_state
) -> None:
    app = _build_app(db_uri, tmp_path, extension_state=team_state, server_config={})
    async with _client(app) as client:
        catalog = await client.get("/v1/extensions")
        detail = await client.get("/v1/extensions/acme.incident-response")
        diagnostics = await client.get("/v1/extensions/diagnostics")
    assert catalog.json()["data"] == []
    assert detail.status_code == 404
    assert diagnostics.json()["disabled_extensions"] == ["acme.incident-response"]


async def test_team_agent_download_and_install_are_content_addressed_and_owned(
    db_uri, tmp_path, team_state
) -> None:
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(_USER)
    permissions.ensure_user(_ADMIN, is_admin=True)
    app = _build_app(
        db_uri,
        tmp_path,
        extension_state=team_state,
        server_config=_team_config(),
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )
    async with _client(app, _USER) as client:
        catalog = (await client.get("/v1/extensions")).json()["data"][0]
        assert catalog["owner"] == "team-reliability"
        assert catalog["policies"][0]["id"] == "acme.incident-response.approvals"
        template = catalog["agents"][0]
        assert template["id"] == "acme.incident-response.triage"
        assert "path" not in template
        bundle = await client.get(template["bundle_url"])
        assert bundle.status_code == 200
        assert bundle.headers["etag"] == f'"{template["digest"]}"'
        install_url = template["bundle_url"].removesuffix("/bundle") + "/install"
        installed = await client.post(install_url)
        repeated = await client.post(install_url)
        invalid = await client.post(install_url.replace(template["digest"], "0" * 64))
        own_agents = await client.get("/v1/agents?scope=user")
    assert installed.status_code == 200, installed.text
    assert repeated.status_code == 200
    assert installed.json()["id"] == repeated.json()["id"]
    assert installed.json()["name"] == "acme.incident-response.triage@0.1.0"
    assert {agent["id"] for agent in own_agents.json()["data"]} == {installed.json()["id"]}
    assert invalid.status_code == 409
    async with _client(app, _ADMIN) as client:
        other_agents = await client.get("/v1/agents?scope=user")
        other_detail = await client.get(f"/v1/agents/{installed.json()['id']}")
    assert other_agents.json()["data"] == []
    assert other_detail.status_code == 404


async def test_team_pack_download_and_install_require_authentication(
    db_uri, tmp_path, team_state
) -> None:
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(_USER)
    app = _build_app(
        db_uri,
        tmp_path,
        extension_state=team_state,
        server_config=_team_config(),
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )
    async with _client(app, _USER) as client:
        catalog = (await client.get("/v1/extensions")).json()
        url = catalog["data"][0]["agents"][0]["bundle_url"]
        denied_origin = await client.post(
            url.removesuffix("/bundle") + "/install",
            headers={"Origin": "https://untrusted.example"},
        )
    async with _client(app) as client:
        download = await client.get(url)
        install = await client.post(url.removesuffix("/bundle") + "/install")
    assert download.status_code == 401
    assert install.status_code == 401
    assert denied_origin.status_code == 403


async def test_disabled_pack_removes_downloads_but_retains_owned_agent_copy(
    db_uri, tmp_path, team_state
) -> None:
    app = _build_app(
        db_uri,
        tmp_path,
        extension_state=team_state,
        server_config=_team_config(),
    )
    async with _client(app) as client:
        catalog = (await client.get("/v1/extensions")).json()["data"][0]
        url = catalog["agents"][0]["bundle_url"]
        installed = await client.post(url.removesuffix("/bundle") + "/install")
        assert installed.status_code == 200, installed.text
    disabled = _build_app(
        db_uri,
        tmp_path,
        extension_state=team_state,
        server_config=_team_config(False),
    )
    async with _client(disabled) as client:
        catalog = await client.get("/v1/extensions")
        download = await client.get(url)
        install = await client.post(url.removesuffix("/bundle") + "/install")
        own = await client.get(f"/v1/agents/{installed.json()['id']}")
    assert catalog.json()["data"] == []
    assert download.status_code == 404
    assert install.status_code == 404
    assert own.status_code == 200


async def test_extension_upgrade_installs_new_agent_without_updating_old_copy(
    db_uri, tmp_path, team_state
) -> None:
    app = _build_app(db_uri, tmp_path, extension_state=team_state, server_config=_team_config())
    async with _client(app) as client:
        template = (await client.get("/v1/extensions")).json()["data"][0]["agents"][0]
        original = await client.post(template["bundle_url"].removesuffix("/bundle") + "/install")
        assert original.status_code == 200, original.text
    store = SqlAlchemyAgentStore(db_uri)
    original_location = store.get(original.json()["id"]).bundle_location
    upgraded_manifest = replace(team_state.manifests[0], version="0.2.0")
    upgraded_state = replace(team_state, manifests=(upgraded_manifest,))
    config = _team_config()
    config["extensions"][upgraded_manifest.id]["version"] = "==0.2.0"
    upgraded = _build_app(
        db_uri,
        tmp_path,
        extension_state=upgraded_state,
        server_config=config,
    )
    async with _client(upgraded) as client:
        template = (await client.get("/v1/extensions")).json()["data"][0]["agents"][0]
        latest = await client.post(template["bundle_url"].removesuffix("/bundle") + "/install")
        assert latest.status_code == 200, latest.text
    assert original.json()["id"] != latest.json()["id"]
    assert store.get(original.json()["id"]).bundle_location == original_location


async def test_quarantined_extension_disappears_from_catalog_and_downloads(
    db_uri, tmp_path, team_state
) -> None:
    app = _build_app(db_uri, tmp_path, extension_state=team_state, server_config=_team_config())
    async with _client(app) as client:
        template = (await client.get("/v1/extensions")).json()["data"][0]["agents"][0]
        app.state.extension_manager._reject("acme.incident-response", "synthetic failure")
        assert (await client.get("/v1/extensions")).json()["data"] == []
        assert (await client.get(template["bundle_url"])).status_code == 404


@pytest.fixture
def catalog_app(db_uri: str, tmp_path: Path) -> FastAPI:
    return _build_app(db_uri, tmp_path, extension_state=_state())


@pytest.fixture
def multi_user_app(db_uri: str, tmp_path: Path) -> FastAPI:
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user(_ADMIN, is_admin=True)
    permission_store.ensure_user(_USER)
    return _build_app(
        db_uri,
        tmp_path,
        extension_state=_state(),
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


async def test_catalog_serializes_only_public_v1_contributions(catalog_app: FastAPI) -> None:
    async with _client(catalog_app) as client:
        response = await client.get("/v1/extensions")

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "object": "extension",
                "id": "acme.review",
                "display_name": "Acme Review",
                "distribution": "omnigent-acme-review",
                "version": "1.2.0",
                "extension_api": 1,
                "owner": None,
                "agents": [],
                "policies": [],
                "status": "unavailable",
                "permissions": ["navigation", "sessions.read", "storage.user"],
                "pages": [],
                "primary_navigation": [],
                "browser": {
                    "declared": True,
                    "has_styles": True,
                    "digest": None,
                    "script_url": None,
                    "style_url": None,
                },
            }
        ],
    }
    assert "commands" not in response.text
    assert "dist/extension.js" not in response.text
    assert "extension import failed" not in response.text


async def test_empty_catalog_on_core_only_install(db_uri: str, tmp_path: Path) -> None:
    app = _build_app(
        db_uri,
        tmp_path,
        extension_state=ExtensionPluginState(manifests=(), load_errors={}),
    )

    async with _client(app) as client:
        response = await client.get("/v1/extensions")

    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


async def test_catalog_order_is_stable(db_uri: str, tmp_path: Path) -> None:
    zeta = replace(_manifest("zeta.review"), entrypoints=ExtensionEntrypoints())
    alpha_page = PageContribution(
        id="acme.review.alpha",
        title="Alpha",
        route="alpha",
        view="alpha",
    )
    alpha_nav = PrimaryNavigationContribution(
        id="acme.review.alpha-nav",
        label="Alpha",
        page=alpha_page.id,
        order=350,
    )
    alpha = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(),
        pages=(*_manifest().pages, alpha_page),
        primary_navigation=(*_manifest().primary_navigation, alpha_nav),
    )
    app = _build_app(
        db_uri,
        tmp_path,
        extension_state=ExtensionPluginState(manifests=(zeta, alpha), load_errors={}),
    )

    async with _client(app) as client:
        data = (await client.get("/v1/extensions")).json()["data"]

    assert [item["id"] for item in data] == ["acme.review", "zeta.review"]
    assert [item["id"] for item in data[0]["pages"]] == [
        "acme.review.alpha",
        "acme.review.dashboard",
    ]
    assert [item["id"] for item in data[0]["primary_navigation"]] == [
        "acme.review.alpha-nav",
        "acme.review.primary-nav",
    ]


async def test_extension_detail_and_unknown_id(catalog_app: FastAPI) -> None:
    async with _client(catalog_app) as client:
        found = await client.get("/v1/extensions/acme.review")
        missing = await client.get("/v1/extensions/missing.extension")

    assert found.status_code == 200
    assert found.json()["id"] == "acme.review"
    assert missing.status_code == 404


async def test_diagnostics_allows_single_user_mode(catalog_app: FastAPI) -> None:
    async with _client(catalog_app) as client:
        response = await client.get("/v1/extensions/diagnostics")

    assert response.status_code == 200
    assert response.json()["load_errors"] == [
        {
            "entry_point": "broken:entry",
            "status": "rejected",
            "error": "extension import failed",
        }
    ]
    assert response.json()["asset_errors"] == [
        {
            "extension_id": "acme.review",
            "status": "unresolved",
            "error": "extension 'acme.review' has no verified asset package",
        }
    ]


async def test_public_catalog_requires_authentication(multi_user_app: FastAPI) -> None:
    async with _client(multi_user_app) as client:
        listing = await client.get("/v1/extensions")
        detail = await client.get("/v1/extensions/acme.review")

    assert listing.status_code == 401
    assert detail.status_code == 401


async def test_diagnostics_requires_auth_without_permission_store(
    db_uri: str,
    tmp_path: Path,
) -> None:
    app = _build_app(
        db_uri,
        tmp_path,
        extension_state=_state(),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )

    async with _client(app) as client:
        response = await client.get("/v1/extensions/diagnostics")

    assert response.status_code == 401


async def test_diagnostics_sanitizes_and_bounds_plugin_errors(
    db_uri: str,
    tmp_path: Path,
) -> None:
    error = "bad\npath\x00" + "x" * 600
    app = _build_app(
        db_uri,
        tmp_path,
        extension_state=ExtensionPluginState(manifests=(), load_errors={"bad": error}),
    )

    async with _client(app) as client:
        response = await client.get("/v1/extensions/diagnostics")

    returned = response.json()["load_errors"][0]["error"]
    assert returned.startswith("bad path ")
    assert len(returned) == 512
    assert "\n" not in returned
    assert "\x00" not in returned


async def test_diagnostics_allows_admin(multi_user_app: FastAPI) -> None:
    async with _client(multi_user_app, _ADMIN) as client:
        response = await client.get("/v1/extensions/diagnostics")

    assert response.status_code == 200


async def test_diagnostics_forbids_non_admin(multi_user_app: FastAPI) -> None:
    async with _client(multi_user_app, _USER) as client:
        response = await client.get("/v1/extensions/diagnostics")

    assert response.status_code == 403


async def test_diagnostics_requires_authentication(multi_user_app: FastAPI) -> None:
    async with _client(multi_user_app) as client:
        response = await client.get("/v1/extensions/diagnostics")

    assert response.status_code == 401


async def test_registry_is_resolved_once_at_app_construction(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def load_state() -> ExtensionPluginState:
        nonlocal calls
        calls += 1
        return _state()

    monkeypatch.setattr(app_module, "load_extension_plugin_state", load_state)
    app = _build_app(db_uri, tmp_path)

    async with _client(app) as client:
        assert (await client.get("/v1/extensions")).status_code == 200
        assert (await client.get("/v1/extensions")).status_code == 200

    assert calls == 1


def test_development_bundle_overrides_are_explicit_and_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = ExtensionPluginState(manifests=())
    captured = None

    def build_index(_state, *, overrides=None):
        nonlocal captured
        captured = overrides
        return {}, {}

    monkeypatch.setenv(
        "OMNIGENT_EXTENSION_DEV_BUNDLES",
        f'{{"acme.review": "{tmp_path}"}}',
    )
    monkeypatch.setattr(app_module, "build_asset_index", build_index)

    with caplog.at_level("WARNING"):
        app_module._resolve_extension_assets(state)

    assert captured == {"acme.review": tmp_path}
    assert "using development extension bundle overrides" in caplog.text


async def test_global_discovery_failure_does_not_stop_server(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_discovery() -> ExtensionPluginState:
        raise RuntimeError("broken distribution metadata")

    monkeypatch.setattr(app_module, "load_extension_plugin_state", fail_discovery)
    app = _build_app(db_uri, tmp_path)

    async with _client(app) as client:
        version = await client.get("/api/version")
        extensions = await client.get("/v1/extensions")
        diagnostics = await client.get("/v1/extensions/diagnostics")

    assert version.status_code == 200
    assert extensions.json() == {"object": "list", "data": []}
    assert diagnostics.json()["load_errors"][0]["error"] == "broken distribution metadata"


def test_extension_routes_are_mounted_before_spa_fallback(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_dist = tmp_path / "web-ui"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", web_dist)
    app = _build_app(db_uri, tmp_path, extension_state=_state())
    paths = [route.path for route in app.routes if hasattr(route, "path")]

    expected = {
        "/v1/extensions",
        "/v1/extensions/diagnostics",
        "/v1/extensions/{extension_id}",
        "/v1/extensions/{extension_id}/assets/{digest}/{asset_name}",
    }
    assert expected <= set(paths)
    spa_index = next(index for index, path in enumerate(paths) if path == "")
    assert all(paths.index(path) < spa_index for path in expected)
