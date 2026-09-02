from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.entities import IntegrationSyncRun
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.company_brain import _run_response
from omnigent.server.routes.session_mcp_servers import _summary_from_config
from omnigent.server.routes.sessions.routes_agent import _runner_can_receive_managed_config
from omnigent.spec.types import MCPServerConfig
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.company_brain_store.sqlalchemy_store import SqlAlchemyCompanyBrainStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class _HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def company_brain_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user("admin@example.com", is_admin=True)
    permission_store.ensure_user("member@example.com", is_admin=False)
    company_brain_store = SqlAlchemyCompanyBrainStore(db_uri)
    company_brain_store.create_connection(
        _uid("route-connection"),
        provider="notion",
        credential_ciphertext="v1:primary:nonce:ciphertext-secret",
        account_label="Policies",
        granted_scopes=(),
        created_by="admin@example.com",
    )
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        permission_store=permission_store,
        auth_provider=_HeaderAuth(),
        company_brain_store=company_brain_store,
    )


@pytest_asyncio.fixture()
async def company_brain_client(
    company_brain_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=company_brain_app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_company_brain_routes_return_403_for_non_admin(
    company_brain_client: httpx.AsyncClient,
) -> None:
    response = await company_brain_client.get(
        "/v1/company-brain",
        headers={"x-test-user": "member@example.com"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_company_brain_status_is_redacted_for_admin(
    company_brain_client: httpx.AsyncClient,
) -> None:
    response = await company_brain_client.get(
        "/v1/company-brain",
        headers={"x-test-user": "admin@example.com"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["connections"][0]["provider"] == "notion"
    assert "credential_ciphertext" not in payload["connections"][0]
    assert "ciphertext-secret" not in response.text


@pytest.mark.asyncio
async def test_provider_contract_excludes_personal_scopes(
    company_brain_client: httpx.AsyncClient,
) -> None:
    response = await company_brain_client.get(
        "/v1/company-brain/providers",
        headers={"x-test-user": "admin@example.com"},
    )

    assert response.status_code == 200
    providers = {item["id"]: item for item in response.json()["providers"]}
    assert all("gmail" not in scope for scope in providers["google"]["scopes"])
    assert "groups:history" not in providers["slack"]["scopes"]
    assert "im:history" not in providers["slack"]["scopes"]


@pytest.mark.asyncio
async def test_activate_rejects_duplicate_resources_before_writes(
    company_brain_client: httpx.AsyncClient,
) -> None:
    resource = {
        "id": "page-1",
        "name": "Retention policy",
        "resource_type": "notion_page",
        "source_url": "https://www.notion.so/page-1",
        "org_shared": True,
        "metadata": {},
    }
    response = await company_brain_client.post(
        f"/v1/company-brain/connections/{_uid('route-connection')}/activate",
        headers={"x-test-user": "admin@example.com"},
        json={"resources": [resource, resource], "timezone": "UTC"},
    )

    assert response.status_code == 422


def test_managed_mcp_headers_require_the_bound_runner_token() -> None:
    token = "runner-binding-secret"
    runner_id = token_bound_runner_id(token)

    assert _runner_can_receive_managed_config(token, runner_id, None) is True
    assert _runner_can_receive_managed_config("wrong-token", runner_id, None) is False
    assert _runner_can_receive_managed_config("", runner_id, None) is False
    assert _runner_can_receive_managed_config(token, "another-runner", frozenset({token})) is True


def test_managed_mcp_summary_omits_endpoint_and_headers() -> None:
    summary = _summary_from_config(
        MCPServerConfig(
            name="company-brain",
            transport="http",
            url="https://brain.internal/mcp",
            headers={"Authorization": "Bearer runner-only-token"},
        )
    )

    assert summary.url is None
    assert summary.headers == {}


def test_sync_run_response_exposes_only_sanitized_index_health() -> None:
    run = IntegrationSyncRun(
        id="run-1",
        workspace_id=0,
        connection_id="connection-1",
        selection_id="selection-1",
        status="succeeded",
        trigger="manual",
        fetched_count=1,
        changed_count=1,
        deleted_count=0,
        skipped_count=0,
        commit_sha="a" * 40,
        gbrain_result={
            "source_health": {
                "source_id": "company-shared",
                "fresh": True,
                "last_sync_at": "2026-08-29T10:00:00.000Z",
                "lag_seconds": 0,
                "total_pages": 1,
                "total_chunks": 1,
                "embedded_chunks": 1,
                "embed_coverage_pct": 100,
                "failed_jobs_24h": 0,
                "queue_depth": 0,
                "sync_running": False,
                "local_path": "/private/company-brain/repo",
            },
            "database_url": "postgresql://secret",
        },
        error=None,
        scheduled_at=None,
        started_at=1,
        finished_at=2,
        created_at=1,
    )

    response = _run_response(run)

    assert response["index_health"]["fresh"] is True
    assert response["index_health"]["queue_depth"] == 0
    assert "local_path" not in response["index_health"]
    assert "gbrain_result" not in response
