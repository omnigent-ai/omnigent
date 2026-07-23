from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from omnigent.entities import Conversation
from omnigent.runtime import get_runner_router, set_runner_router
from omnigent.server.artifact_previews import (
    ArtifactPreviewHostMiddleware,
    ArtifactPreviewNotFound,
    ArtifactPreviewService,
    create_artifact_preview_public_router,
)
from omnigent.server.routes.sessions import create_sessions_router


def _runner_client() -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/v1/sessions/conv_preview/artifact-preview/")
        return httpx.Response(
            200,
            content=b"console.log('preview')",
            headers={"content-type": "text/javascript; charset=utf-8"},
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://runner",
    )


@pytest.mark.asyncio
async def test_grant_uses_unprivileged_origin_and_high_entropy_token() -> None:
    client = _runner_client()

    async def resolve(_session_id: str) -> httpx.AsyncClient:
        return client

    service = ArtifactPreviewService(
        preview_origin="http://preview.localhost:6767",
        runner_client_for_session=resolve,
    )
    grant = await service.create_grant(
        "conv_preview",
        "artifacts/revenue/index.html",
    )

    assert grant.url.startswith("http://preview.localhost:6767/p/")
    assert grant.url.endswith("/artifacts/revenue/index.html")
    assert len(grant.token) >= 43
    await client.aclose()


@pytest.mark.asyncio
async def test_grant_blocks_cross_artifact_resource_reads() -> None:
    client = _runner_client()

    async def resolve(_session_id: str) -> httpx.AsyncClient:
        return client

    service = ArtifactPreviewService(
        preview_origin="http://preview.localhost:6767",
        runner_client_for_session=resolve,
    )
    grant = await service.create_grant(
        "conv_preview",
        "artifacts/revenue/index.html",
    )

    with pytest.raises(ArtifactPreviewNotFound):
        await service.read(grant.token, "artifacts/other/index.html")
    await client.aclose()


@pytest.mark.asyncio
async def test_grant_proxies_same_root_resources_without_credentials() -> None:
    client = _runner_client()

    async def resolve(_session_id: str) -> httpx.AsyncClient:
        return client

    service = ArtifactPreviewService(
        preview_origin="http://preview.localhost:6767",
        runner_client_for_session=resolve,
    )
    grant = await service.create_grant(
        "conv_preview",
        "artifacts/revenue/index.html",
    )

    resource = await service.read(grant.token, "artifacts/revenue/app.js")

    assert resource.content == b"console.log('preview')"
    assert resource.content_type == "text/javascript; charset=utf-8"
    await client.aclose()


def test_authenticated_session_broker_returns_capability_url() -> None:
    runner_client = _runner_client()

    async def resolve(_session_id: str) -> httpx.AsyncClient:
        return runner_client

    service = ArtifactPreviewService(
        preview_origin="http://preview.localhost:6767",
        runner_client_for_session=resolve,
    )

    class ConversationStore:
        def get_conversation(self, conversation_id: str) -> Conversation | None:
            if conversation_id != "conv_preview":
                return None
            return Conversation(
                id=conversation_id,
                created_at=1,
                updated_at=1,
                root_conversation_id=conversation_id,
                title="Preview",
                agent_id="ag_willy",
            )

    class AgentStore:
        pass

    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=ConversationStore(),  # type: ignore[arg-type]
            agent_store=AgentStore(),  # type: ignore[arg-type]
            artifact_preview_service=service,
        ),
        prefix="/v1",
    )
    response = TestClient(app).post(
        "/v1/sessions/conv_preview/artifact-previews",
        json={"entry_path": "artifacts/revenue/index.html"},
    )

    assert response.status_code == 201
    assert response.json()["url"].startswith("http://preview.localhost:6767/p/")
    assert response.json()["expires_at"] > 0


def test_authenticated_session_lists_runner_managed_artifacts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_preview/artifacts"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "path": "artifacts/revenue/index.html",
                        "name": "index.html",
                        "type": "file",
                        "bytes": 200,
                        "modified_at": 2,
                    }
                ],
                "has_more": False,
            },
            request=request,
        )

    runner_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://runner",
    )

    class RunnerRouter:
        def client_for_session_resources(self, session_id: str) -> SimpleNamespace:
            assert session_id == "conv_preview"
            return SimpleNamespace(client=runner_client)

    class ConversationStore:
        def get_conversation(self, conversation_id: str) -> Conversation | None:
            if conversation_id != "conv_preview":
                return None
            return Conversation(
                id=conversation_id,
                created_at=1,
                updated_at=1,
                root_conversation_id=conversation_id,
                title="Preview",
                agent_id="ag_willy",
            )

    prior_router = get_runner_router()
    set_runner_router(RunnerRouter())  # type: ignore[arg-type]
    try:
        app = FastAPI()
        app.include_router(
            create_sessions_router(
                conversation_store=ConversationStore(),  # type: ignore[arg-type]
                agent_store=object(),  # type: ignore[arg-type]
            ),
            prefix="/v1",
        )
        response = TestClient(app).get("/v1/sessions/conv_preview/artifacts")
    finally:
        set_runner_router(prior_router)

    assert response.status_code == 200
    assert response.json()["data"][0]["path"] == "artifacts/revenue/index.html"


def test_preview_hostname_cannot_reach_application_routes() -> None:
    runner_client = _runner_client()

    async def resolve(_session_id: str) -> httpx.AsyncClient:
        return runner_client

    service = ArtifactPreviewService(
        preview_origin="http://preview.localhost:6767",
        runner_client_for_session=resolve,
    )
    app = FastAPI()
    app.add_middleware(
        ArtifactPreviewHostMiddleware,
        preview_hostname=service.preview_hostname,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(create_artifact_preview_public_router(service))
    client = TestClient(app)

    assert (
        client.get(
            "/health",
            headers={"host": "preview.localhost:6767"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/p/not-a-grant/artifacts/revenue/index.html",
            headers={"host": "localhost:6767"},
        ).status_code
        == 404
    )


@pytest.mark.asyncio
async def test_preview_response_has_strict_headers_and_supports_head() -> None:
    runner_client = _runner_client()

    async def resolve(_session_id: str) -> httpx.AsyncClient:
        return runner_client

    service = ArtifactPreviewService(
        preview_origin="http://preview.localhost:6767",
        runner_client_for_session=resolve,
    )
    grant = await service.create_grant(
        "conv_preview",
        "artifacts/revenue/index.html",
    )
    app = FastAPI()
    app.include_router(create_artifact_preview_public_router(service))
    path = grant.url.removeprefix("http://preview.localhost:6767")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://preview.localhost:6767",
    ) as client:
        response = await client.get(path)
        head = await client.head(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "form-action 'none'" in response.headers["content-security-policy"]
    assert head.status_code == 200
    assert head.content == b""
    await runner_client.aclose()
