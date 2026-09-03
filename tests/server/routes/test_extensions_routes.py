"""Tests for the installed-extension catalog and diagnostics routes."""

from __future__ import annotations

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
        permissions=frozenset({ExtensionPermission.NAVIGATION, ExtensionPermission.STORAGE_USER}),
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
    )


def _client(app: FastAPI, email: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": email} if email else {},
    )


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
                "status": "unavailable",
                "permissions": ["navigation", "storage.user"],
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
