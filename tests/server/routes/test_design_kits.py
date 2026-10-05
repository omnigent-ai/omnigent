"""Routes for the New design default preference and the organization design kit."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes.design import create_design_router
from omnigent.server.server_config import BrandingAsset, DesignKitSnapshot
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

_URL = "/v1/me/preferences/design-default"
_SYSTEM = {"kind": "full", "host_id": "h1", "path": "/ds/brand", "name": "Brand"}


class _HeaderAuth:
    def get_user_id(self, request: object) -> str | None:
        return getattr(request, "headers", {}).get("x-test-user")


_LOGO = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
_KIT = DesignKitSnapshot(
    name="Acme Kit",
    assets={
        "kit.json": BrandingAsset(
            path=Path("/kit/kit.json"), media_type="application/json", content=b'{"name":"A"}'
        ),
        "img/logo.svg": BrandingAsset(
            path=Path("/kit/img/logo.svg"), media_type="image/svg+xml", content=_LOGO
        ),
    },
)


def _app(
    db_uri: str, *, enabled: bool = True, design_kit: DesignKitSnapshot | None = _KIT
) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code}})

    flags = FeatureFlags(frozenset({Feature.DESIGN}) if enabled else frozenset())
    app.include_router(
        create_design_router(
            SqlAlchemyConversationStore(db_uri),
            None,
            auth_provider=_HeaderAuth(),
            feature_flags=flags,
            project_store=SqlAlchemyProjectStore(db_uri),
            design_kit=design_kit,
        ),
        prefix="/v1",
    )
    return app


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server"
    ) as client:
        yield client


@pytest.fixture
async def client(db_uri: str) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(_app(db_uri)):
        yield c


def _as(user: str) -> dict[str, str]:
    return {"x-test-user": user}


async def test_design_default_requires_sign_in(client: httpx.AsyncClient) -> None:
    assert (await client.get(_URL)).status_code == 401
    put = await client.put(_URL, json={"design_default": _SYSTEM})
    assert put.status_code == 401


async def test_design_default_round_trip_per_user(client: httpx.AsyncClient) -> None:
    assert (await client.get(_URL, headers=_as("alice"))).json() == {"design_default": None}

    saved = await client.put(_URL, json={"design_default": _SYSTEM}, headers=_as("alice"))
    assert saved.status_code == 200
    assert saved.json() == {"design_default": _SYSTEM}
    assert (await client.get(_URL, headers=_as("alice"))).json() == {"design_default": _SYSTEM}
    assert (await client.get(_URL, headers=_as("bob"))).json() == {"design_default": None}

    none = {"design_default": {"kind": "none"}}
    assert (await client.put(_URL, json=none, headers=_as("alice"))).json() == none
    assert (await client.get(_URL, headers=_as("alice"))).json() == none

    cleared = await client.put(_URL, json={"design_default": None}, headers=_as("alice"))
    assert cleared.json() == {"design_default": None}
    assert (await client.get(_URL, headers=_as("alice"))).json() == {"design_default": None}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"design_default": {"kind": "kit"}},
        {"design_default": {"kind": "none", "path": "/ds"}},
        {"design_default": {**_SYSTEM, "host_id": ""}},
        {"design_default": {**_SYSTEM, "path": "/ds/../secret"}},
        {"design_default": {**_SYSTEM, "path": "C:\\ds\\.\\brand"}},
        {"design_default": {**_SYSTEM, "path": "/ds/\u0000x"}},
        {"design_default": {**_SYSTEM, "name": "x" * 81}},
        {"design_default": {k: v for k, v in _SYSTEM.items() if k != "path"}},
    ],
)
async def test_design_default_rejects_invalid_values(
    client: httpx.AsyncClient, body: dict[str, object]
) -> None:
    await client.put(_URL, json={"design_default": _SYSTEM}, headers=_as("alice"))
    assert (await client.put(_URL, json=body, headers=_as("alice"))).status_code == 422
    assert (await client.get(_URL, headers=_as("alice"))).json() == {"design_default": _SYSTEM}


async def test_invalid_stored_design_default_reads_as_unset(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    SqlAlchemyProjectStore(db_uri).save_design_default(
        {"kind": "full", "host_id": "h1", "path": "../x", "name": "X"}, user_id="alice"
    )
    assert (await client.get(_URL, headers=_as("alice"))).json() == {"design_default": None}


async def test_design_default_is_hidden_with_the_flag_off(db_uri: str) -> None:
    async for client in _client(_app(db_uri, enabled=False)):
        assert (await client.get(_URL, headers=_as("alice"))).status_code == 404
        put = await client.put(_URL, json={"design_default": None}, headers=_as("alice"))
        assert put.status_code == 404


async def test_design_kit_asset_requires_sign_in(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/design-kit/kit.json")).status_code == 401


async def test_design_kit_serves_validated_files_with_their_type(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/design-kit/img/logo.svg", headers=_as("alice"))
    assert response.status_code == 200
    assert response.content == _LOGO
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    kit_json = await client.get("/v1/design-kit/kit.json", headers=_as("alice"))
    assert kit_json.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize(
    "path",
    [
        "",
        "img",
        "img/",
        "missing.css",
        "../config.yaml",
        "%2e%2e/config.yaml",
        "img%2F..%2F..%2Fx",
    ],
)
async def test_design_kit_serves_nothing_outside_the_validated_set(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(f"/v1/design-kit/{path}", headers=_as("alice"))
    assert response.status_code == 404


async def test_design_kit_404s_when_unset_or_flag_off(db_uri: str) -> None:
    for app in (_app(db_uri, design_kit=None), _app(db_uri, enabled=False)):
        async for client in _client(app):
            response = await client.get("/v1/design-kit/kit.json", headers=_as("alice"))
            assert response.status_code == 404
