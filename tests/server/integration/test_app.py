"""Integration tests for app-level routes."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as app_module
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio


async def test_root_serves_html_landing_without_web_ui(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no web UI bundle, ``GET /`` serves a friendly HTML landing page
    (status 200) explaining the API-only state — not JSON metadata.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", tmp_path / "missing-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "web UI" in resp.text
    assert "OMNIGENT_SKIP_WEB_UI" in resp.text


async def test_web_ui_static_files_send_cache_control_headers(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The SPA static mount advertises browser caching for cacheable assets.

    This exercises the real ``StaticFiles`` mount rather than a helper:
    ``/`` and extensionless routes return the HTML shell with revalidation,
    hashed Vite assets under ``assets/`` are immutable, and non-hashed
    static files receive a short cache lifetime.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    web_ui_dist = tmp_path / "web-ui"
    assets_dir = web_ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    (web_ui_dist / "index.html").write_text("<!doctype html><div id='root'></div>")
    (assets_dir / "index-AbCd1234.js").write_text("console.log('cached');")
    (assets_dir / "large-AbCd1234.js").write_text(
        "const payload = '" + ("x" * app_module._WEB_UI_GZIP_MINIMUM_SIZE) + "';"
    )
    (web_ui_dist / "favicon.ico").write_bytes(b"\0\0ico")

    monkeypatch.setattr(app_module, "_WEB_UI_DIST", web_ui_dist)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        fallback = await client.get("/c/session_123")
        asset = await client.get("/assets/index-AbCd1234.js")
        large_asset = await client.get(
            "/assets/large-AbCd1234.js",
            headers={"Accept-Encoding": "gzip"},
        )
        ranged_large_asset = await client.get(
            "/assets/large-AbCd1234.js",
            headers={"Accept-Encoding": "gzip", "Range": "bytes=0-19"},
        )
        icon = await client.get("/favicon.ico")
        root_not_modified = await client.get(
            "/",
            headers={"If-None-Match": root.headers["etag"]},
        )
        fallback_not_modified = await client.get(
            "/c/session_123",
            headers={"If-None-Match": fallback.headers["etag"]},
        )
        asset_not_modified = await client.get(
            "/assets/index-AbCd1234.js",
            headers={"If-None-Match": asset.headers["etag"]},
        )

    assert root.status_code == 200
    assert fallback.status_code == 200
    assert asset.status_code == 200
    assert large_asset.status_code == 200
    assert ranged_large_asset.status_code == 206
    assert icon.status_code == 200
    assert root_not_modified.status_code == 304
    assert fallback_not_modified.status_code == 304
    assert asset_not_modified.status_code == 304
    assert root.headers["etag"]
    assert fallback.headers["etag"] == root.headers["etag"]
    assert asset.headers["etag"]
    assert large_asset.headers["etag"]
    assert root.headers["cache-control"] == app_module._WEB_UI_HTML_CACHE_CONTROL
    assert fallback.headers["cache-control"] == app_module._WEB_UI_HTML_CACHE_CONTROL
    assert asset.headers["cache-control"] == app_module._WEB_UI_ASSET_CACHE_CONTROL
    assert large_asset.headers["cache-control"] == app_module._WEB_UI_ASSET_CACHE_CONTROL
    assert icon.headers["cache-control"] == app_module._WEB_UI_STATIC_CACHE_CONTROL
    assert "content-encoding" not in asset.headers
    assert large_asset.headers["content-encoding"] == "gzip"
    assert large_asset.headers["vary"] == "Accept-Encoding"
    assert "content-encoding" not in ranged_large_asset.headers
    assert ranged_large_asset.headers["content-range"].startswith("bytes 0-19/")
    assert ranged_large_asset.content == b"const payload = 'xxx"
    assert root_not_modified.headers["cache-control"] == app_module._WEB_UI_HTML_CACHE_CONTROL
    assert fallback_not_modified.headers["cache-control"] == app_module._WEB_UI_HTML_CACHE_CONTROL
    assert asset_not_modified.headers["cache-control"] == app_module._WEB_UI_ASSET_CACHE_CONTROL


async def test_host_routes_not_mounted_without_host_store(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no host_store configured, the host tunnel + REST routers are not
    mounted at all — rather than mounted with a None store, which would
    AttributeError (swallowed by the tunnel's broad except) on every host
    connection. GET /v1/hosts therefore 404s.
    """
    # Disable the SPA fallback so an unmounted /v1/* path 404s instead of
    # being served index.html.
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", tmp_path / "missing-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        # host_store intentionally omitted.
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/hosts")
    assert resp.status_code == 404, (
        "host routes must not be mounted when host_store is None — a "
        f"mounted-but-broken router would AttributeError. Got {resp.status_code}."
    )


async def test_host_routes_mounted_with_host_store(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a host_store configured, the host REST routes are mounted."""
    from omnigent.stores.host_store import HostStore

    monkeypatch.setattr(app_module, "_WEB_UI_DIST", tmp_path / "missing-web-ui")
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        host_store=HostStore(db_uri),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/hosts")
    # Mounted → 200 with an (empty) host list, not 404.
    assert resp.status_code == 200, (
        f"host routes should be mounted when host_store is set; got {resp.status_code}."
    )
    assert resp.json() == {"hosts": []}


async def test_me_header_mode_behaviors(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header-mode auth: reject missing header, accept valid, reject reserved.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    :param monkeypatch: Pytest monkeypatch fixture — pins
        ``OMNIGENT_AUTH_PROVIDER=header`` explicitly so an ambient
        ``OMNIGENT_AUTH_ENABLED=1`` in the shell can't flip this
        test into accounts mode (header is the env-unset default, but the
        explicit pin guarantees it), and clears
        ``OMNIGENT_LOCAL_SINGLE_USER`` so the strict (deployed
        multi-user) posture is under test.
    """
    from omnigent.server.auth import create_auth_provider

    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    auth_provider = create_auth_provider()
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=auth_provider,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/v1/me")
        normal = await client.get(
            "/v1/me",
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
        reserved = await client.get(
            "/v1/me",
            headers={"X-Forwarded-Email": "local"},
        )

    # Missing header fails closed: /v1/me itself stays
    # 200 (it's the identity probe the frontend bootstraps from) but
    # reports no user instead of resolving to a shared "local" identity.
    # is_admin is always present (mode-agnostic signal) and false with no user.
    assert missing.status_code == 200
    assert missing.json() == {"user_id": None, "is_admin": False}
    # Valid header returns the identity; alice has no admin flag set.
    assert normal.status_code == 200
    assert normal.json() == {"user_id": "alice@example.com", "is_admin": False}
    # Reserved name is rejected (returns None → route returns null).
    assert reserved.status_code == 200
    assert reserved.json() == {"user_id": None, "is_admin": False}


async def test_me_is_admin_honors_admin_list_before_db_promotion(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/v1/me`` reports is_admin for an admin-list identity not yet promoted.

    The admin-gated routes (``/auth/users``, ``/auth/invite``) authorize on
    ``permission_store.is_admin(caller) or admin_list.is_admin(caller)``. If
    ``/v1/me`` checked only the DB flag, an identity freshly added to the
    admin-list file (before ``promote_if_listed`` runs at their next login)
    would be authorized by those routes yet see no admin chrome. This pins
    ``/v1/me`` to the same fallback so the SPA never under-reports.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    :param monkeypatch: Pins header auth and points the admin-list file at a
        roster containing ``alice`` — whose DB row is intentionally left
        non-admin.
    """
    from omnigent.server.auth import create_auth_provider

    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    # Admin-list file lists alice; her DB row is never promoted.
    admin_file = tmp_path / "admins"
    admin_file.write_text("alice@example.com\n")
    monkeypatch.setenv("OMNIGENT_ADMIN_LIST_PATH", str(admin_file))

    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user("alice@example.com")  # is_admin defaults False
    assert perm_store.is_admin("alice@example.com") is False

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        permission_store=perm_store,
        auth_provider=create_auth_provider(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/me",
            headers={"X-Forwarded-Email": "alice@example.com"},
        )

    # DB flag is still False, but the admin-list fallback flips is_admin True —
    # matching what /auth/users would authorize for the same caller.
    assert resp.status_code == 200
    assert resp.json() == {"user_id": "alice@example.com", "is_admin": True}


async def test_web_ui_serves_pwa_service_worker_and_manifest(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    PWA assets are served correctly from the SPA static mount.

    ``sw.js`` must be ``no-cache`` so a deploy is picked up promptly — a
    stale service worker would defeat prompt-to-reload — and
    ``manifest.webmanifest`` must carry ``application/manifest+json`` or the
    browser silently refuses to install the app.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    web_ui_dist = tmp_path / "web-ui"
    web_ui_dist.mkdir(parents=True)
    (web_ui_dist / "index.html").write_text("<!doctype html><div id='root'></div>")
    (web_ui_dist / "sw.js").write_text("self.addEventListener('install', () => {});")
    (web_ui_dist / "manifest.webmanifest").write_text('{"name":"Omnigent"}')
    (web_ui_dist / "version.json").write_text('{"build":"testbuild"}')

    monkeypatch.setattr(app_module, "_WEB_UI_DIST", web_ui_dist)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sw = await client.get("/sw.js")
        manifest = await client.get("/manifest.webmanifest")
        version = await client.get("/version.json")

    assert sw.status_code == 200
    assert sw.headers["cache-control"] == app_module._WEB_UI_HTML_CACHE_CONTROL
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    # version.json is the SW's cache sentinel: if the static mount ever stopped
    # serving it, the SW install would fail and the update prompt never fire. It
    # is no-cache for the same reason as sw.js — a stale sentinel must not linger.
    assert version.status_code == 200
    assert version.headers["cache-control"] == app_module._WEB_UI_HTML_CACHE_CONTROL
