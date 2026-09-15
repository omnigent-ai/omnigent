from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.extensions import (
    ExtensionManifest,
    ExtensionPluginState,
    check_extension_package,
)
from omnigent.extensions.assets import ASSET_SCRIPT, ExtensionAssetError
from omnigent.server.routes.extension_assets import create_extension_assets_router
from omnigent.server.routes.extensions import create_extensions_router

_REPO_ROOT = Path(__file__).parents[2]
_EXAMPLE_ROOT = _REPO_ROOT / "examples" / "extensions" / "hello-page"
_PACKAGE_ROOT = _EXAMPLE_ROOT / "src" / "omnigent_hello_extension"


def _load_manifest() -> ExtensionManifest:
    spec = importlib.util.spec_from_file_location(
        "omnigent_hello_extension.plugin",
        _PACKAGE_ROOT / "plugin.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    return module.get_manifest()


def test_conformance_checks_project_distribution_identity() -> None:
    manifest = replace(_load_manifest(), distribution="wrong-distribution")

    with pytest.raises(ExtensionAssetError, match="does not match pyproject"):
        check_extension_package(
            manifest,
            package_root=_PACKAGE_ROOT,
            project_root=_EXAMPLE_ROOT,
        )


async def test_reference_extension_package_reaches_catalog_and_asset_route() -> None:
    manifest = _load_manifest()
    bundle = check_extension_package(
        manifest,
        package_root=_PACKAGE_ROOT,
        project_root=_EXAMPLE_ROOT,
    )
    assert bundle is not None
    assert b"omnigent-extension" in bundle.assets[ASSET_SCRIPT].content

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    state = ExtensionPluginState(manifests=(manifest,))
    app.include_router(
        create_extensions_router(state, bundles={manifest.id: bundle}),
        prefix="/v1",
    )
    app.include_router(
        create_extension_assets_router({manifest.id: bundle}),
        prefix="/v1",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        catalog = await client.get("/v1/extensions")
        asset_url = catalog.json()["data"][0]["browser"]["script_url"]
        asset = await client.get(asset_url)

    assert catalog.status_code == 200
    assert catalog.json()["data"][0]["primary_navigation"][0]["label"] == "Hello Extension"
    assert asset.status_code == 200
    assert asset.content == bundle.assets[ASSET_SCRIPT].content
