from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.extensions import EXTENSION_API_VERSION, ExtensionEntrypoints, ExtensionManifest
from omnigent.extensions.api import ExtensionPluginState
from omnigent.extensions.assets import ASSET_SCRIPT, ASSET_STYLES, resolve_bundle
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.extension_assets import create_extension_assets_router
from omnigent.server.routes.extensions import create_extensions_router


def _manifest(css: bool = True) -> ExtensionManifest:
    return ExtensionManifest(
        id="acme.assets",
        display_name="Assets",
        distribution="acme-assets",
        version="1.0.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css" if css else None,
        ),
    )


def _bundle(tmp_path: Path, *, js: bytes = b"console.log('ok')", css: bool = True):
    root = tmp_path / "package"
    (root / "dist").mkdir(parents=True, exist_ok=True)
    (root / "dist" / "extension.js").write_bytes(js)
    if css:
        (root / "dist" / "extension.css").write_bytes(b"body{}")
    return resolve_bundle(_manifest(css), root_override=root)


def _app(bundle, *, auth=False) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    provider = UnifiedAuthProvider(source="header", local_single_user=False) if auth else None
    app.include_router(
        create_extensions_router(
            ExtensionPluginState(manifests=(_manifest(ASSET_STYLES in bundle.assets),)),
            bundles={bundle.extension_id: bundle},
            auth_provider=provider,
        ),
        prefix="/v1",
    )
    app.include_router(
        create_extension_assets_router(
            {bundle.extension_id: bundle},
            auth_provider=provider,
        ),
        prefix="/v1",
    )
    return app


def _client(app: FastAPI, *, authenticated: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": "user@example.com"} if authenticated else {},
    )


async def test_catalog_exposes_digest_urls_without_package_paths(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    async with _client(_app(bundle)) as client:
        response = await client.get("/v1/extensions")

    browser = response.json()["data"][0]["browser"]
    assert browser == {
        "declared": True,
        "has_styles": True,
        "digest": bundle.digest,
        "script_url": bundle.url(ASSET_SCRIPT),
        "style_url": bundle.url(ASSET_STYLES),
    }
    assert str(tmp_path) not in response.text
    assert "dist/extension" not in response.text


async def test_serves_script_css_and_conditional_cache(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    async with _client(_app(bundle)) as client:
        script = await client.get(bundle.url(ASSET_SCRIPT))
        styles = await client.get(bundle.url(ASSET_STYLES))
        cached = await client.get(
            bundle.url(ASSET_SCRIPT), headers={"If-None-Match": script.headers["etag"]}
        )
        weak_cached = await client.get(
            bundle.url(ASSET_SCRIPT), headers={"If-None-Match": f"W/{script.headers['etag']}"}
        )

    assert script.status_code == 200
    assert script.content == b"console.log('ok')"
    assert script.headers["content-type"] == "text/javascript; charset=utf-8"
    assert script.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert script.headers["x-content-type-options"] == "nosniff"
    assert script.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert styles.headers["content-type"] == "text/css; charset=utf-8"
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == script.headers["etag"]
    assert weak_cached.status_code == 304


async def test_all_unknown_asset_variants_are_not_found(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, css=False)
    base = f"/v1/extensions/{bundle.extension_id}/assets"
    paths = [
        f"{base}/{'0' * 64}/extension.js",
        f"{base}/{bundle.digest}/extension.css",
        f"{base}/{bundle.digest}/extension.js.map",
        f"/v1/extensions/missing.extension/assets/{bundle.digest}/extension.js",
    ]
    async with _client(_app(bundle)) as client:
        responses = [await client.get(path) for path in paths]

    assert {response.status_code for response in responses} == {404}
    assert {response.json()["error"] for response in responses} == {"Extension asset not found"}


async def test_rejected_extension_has_no_asset_surface(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    state = ExtensionPluginState(
        manifests=(),
        load_errors={"acme-assets:entry": "invalid manifest"},
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.include_router(create_extension_assets_router({}), prefix="/v1")
    app.include_router(create_extensions_router(state), prefix="/v1")

    async with _client(app) as client:
        asset = await client.get(bundle.url(ASSET_SCRIPT))
        diagnostics = await client.get("/v1/extensions/diagnostics")

    assert asset.status_code == 404
    assert diagnostics.json()["load_errors"][0]["status"] == "rejected"


async def test_old_digest_is_rejected_after_bundle_change(tmp_path: Path) -> None:
    old = _bundle(tmp_path, js=b"old")
    new = _bundle(tmp_path, js=b"new")
    async with _client(_app(new)) as client:
        stale = await client.get(old.url(ASSET_SCRIPT))
        current = await client.get(new.url(ASSET_SCRIPT))

    assert stale.status_code == 404
    assert current.status_code == 200
    assert current.content == b"new"


async def test_asset_requires_authentication_in_multi_user_mode(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    app = _app(bundle, auth=True)
    async with _client(app) as anonymous:
        denied = await anonymous.get(bundle.url(ASSET_SCRIPT))
    async with _client(app, authenticated=True) as authenticated:
        allowed = await authenticated.get(bundle.url(ASSET_SCRIPT))

    assert denied.status_code == 401
    assert allowed.status_code == 200
