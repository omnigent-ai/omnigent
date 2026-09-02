from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Literal, Self

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from omnigent_company_brain.oauth import PROVIDERS, ProviderName
from omnigent_company_brain.providers import ProviderResource
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import (
    BrainInstallation,
    IntegrationConnection,
    IntegrationSelection,
    IntegrationSyncRun,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.company_brain_service import CompanyBrainService
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.scheduled.rrule import RRuleValidationError, validate_rrule
from omnigent.stores.company_brain_store import CompanyBrainStore
from omnigent.stores.permission_store import PermissionStore


class OAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str = Field(min_length=1, max_length=2048)
    return_to: str | None = Field(default=None, max_length=2048)


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=512)
    resource_type: str = Field(min_length=1, max_length=64)
    source_url: str | None = Field(default=None, max_length=2048)
    org_shared: Literal[True]
    metadata: dict[str, str] = Field(default_factory=dict)

    def to_entity(self) -> ProviderResource:
        return ProviderResource(
            id=self.id,
            name=self.name,
            resource_type=self.resource_type,
            source_url=self.source_url,
            org_shared=True,
            metadata=self.metadata,
        )


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: tuple[ResourceInput, ...] = Field(min_length=1, max_length=20)


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: tuple[ResourceInput, ...] = Field(min_length=1, max_length=100)
    rrule: str | None = Field(default=None, max_length=512)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)

    @model_validator(mode="after")
    def resources_are_unique(self) -> Self:
        identities = [(resource.resource_type, resource.id) for resource in self.resources]
        if len(set(identities)) != len(identities):
            raise ValueError("resources must not contain duplicates")
        return self


class UpdateSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["active", "paused"] | None = None
    rrule: str | None = Field(default=None, max_length=512)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    admin_list: Any | None,
) -> str:
    user_id = get_user_id(request, auth_provider)
    if permission_store is None:
        return user_id or RESERVED_USER_LOCAL
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
    if admin_list is not None:
        is_admin = is_admin or admin_list.is_admin(user_id)
    if not is_admin:
        raise OmnigentError(
            "Admin privileges required to manage the company brain",
            code=ErrorCode.FORBIDDEN,
        )
    return user_id


def _installation_response(value: BrainInstallation | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "id": value.id,
        "status": value.status,
        "repo_url": value.repo_url,
        "mcp_url": value.mcp_url,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _connection_response(value: IntegrationConnection) -> dict[str, Any]:
    return {
        "id": value.id,
        "provider": value.provider,
        "account_label": value.account_label,
        "granted_scopes": list(value.granted_scopes),
        "status": value.status,
        "last_error": value.last_error,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _selection_response(value: IntegrationSelection) -> dict[str, Any]:
    return {
        "id": value.id,
        "connection_id": value.connection_id,
        "external_resource_id": value.external_resource_id,
        "resource_name": value.resource_name,
        "resource_type": value.resource_type,
        "source_url": value.source_url,
        "transform_profile": value.transform_profile,
        "visibility_class": value.visibility_class,
        "rrule": value.rrule,
        "timezone": value.timezone,
        "state": value.state,
        "last_synced_at": value.last_synced_at,
        "page_count": value.page_count,
        "last_error": value.last_error,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _run_response(value: IntegrationSyncRun) -> dict[str, Any]:
    source_health = None
    if isinstance(value.gbrain_result, dict):
        candidate = value.gbrain_result.get("source_health")
        if isinstance(candidate, dict):
            source_health = {
                key: candidate.get(key)
                for key in (
                    "source_id",
                    "fresh",
                    "last_sync_at",
                    "lag_seconds",
                    "total_pages",
                    "total_chunks",
                    "embedded_chunks",
                    "embed_coverage_pct",
                    "failed_jobs_24h",
                    "queue_depth",
                    "sync_running",
                )
            }
    return {
        "id": value.id,
        "connection_id": value.connection_id,
        "selection_id": value.selection_id,
        "status": value.status,
        "trigger": value.trigger,
        "fetched_count": value.fetched_count,
        "changed_count": value.changed_count,
        "deleted_count": value.deleted_count,
        "skipped_count": value.skipped_count,
        "commit_sha": value.commit_sha,
        "index_health": source_health,
        "error": value.error,
        "scheduled_at": value.scheduled_at,
        "started_at": value.started_at,
        "finished_at": value.finished_at,
        "created_at": value.created_at,
    }


def _service_or_error(service: CompanyBrainService | None) -> CompanyBrainService:
    if service is None:
        raise OmnigentError(
            "Company brain secrets are not configured on this server",
            code=ErrorCode.CONFLICT,
        )
    return service


def _provider(value: str) -> ProviderName:
    if value not in PROVIDERS:
        raise OmnigentError("Unknown company brain provider", code=ErrorCode.NOT_FOUND)
    return value


def create_company_brain_router(
    store: CompanyBrainStore,
    *,
    service: CompanyBrainService | None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    admin_list: Any | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/company-brain")
    async def get_company_brain(request: Request) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        if service is not None:
            await service.ensure_installation()
        installation, connections, selections, runs = await asyncio.gather(
            asyncio.to_thread(store.get_installation),
            asyncio.to_thread(store.list_connections),
            asyncio.to_thread(store.list_selections),
            asyncio.to_thread(store.list_sync_runs, limit=100),
        )
        return {
            "object": "company_brain",
            "installation": _installation_response(installation),
            "connections": [_connection_response(item) for item in connections],
            "selections": [_selection_response(item) for item in selections],
            "runs": [_run_response(item) for item in runs],
        }

    @router.get("/company-brain/providers")
    async def get_providers(request: Request) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        return {
            "providers": [
                {
                    "id": provider.name,
                    "configured": bool(
                        os.environ.get(provider.client_id_env)
                        and os.environ.get(provider.client_credential_env)
                    ),
                    "scopes": list(provider.scopes),
                }
                for provider in PROVIDERS.values()
            ]
        }

    @router.post("/company-brain/oauth/{provider}/start")
    async def start_oauth(
        provider: str,
        request: Request,
        body: OAuthStartRequest,
    ) -> dict[str, str]:
        admin_id = await _require_admin(request, auth_provider, permission_store, admin_list)
        try:
            return _service_or_error(service).begin_oauth(
                _provider(provider),
                admin_id=admin_id,
                redirect_uri=body.redirect_uri,
                return_to=body.return_to,
            )
        except RuntimeError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc

    @router.get("/company-brain/oauth/{provider}/callback")
    async def oauth_callback(
        provider: str,
        request: Request,
        state: str,
        code: str,
    ) -> HTMLResponse:
        admin_id = await _require_admin(request, auth_provider, permission_store, admin_list)
        try:
            connection = await _service_or_error(service).finish_oauth(
                _provider(provider),
                sealed_state=state,
                code=code,
                admin_id=admin_id,
            )
        except (ValueError, RuntimeError) as exc:
            raise OmnigentError(
                "OAuth callback was rejected", code=ErrorCode.INVALID_INPUT
            ) from exc
        payload = json.dumps(
            {
                "type": "company-brain.oauth.connected",
                "provider": connection.provider,
                "connectionId": connection.id,
            }
        ).replace("<", "\\u003c")
        return HTMLResponse(
            "<!doctype html><html><body><p>Source connected. This window can close.</p>"
            f"<script>window.opener?.postMessage({payload}, window.location.origin);"
            "window.close();</script></body></html>"
        )

    @router.get("/company-brain/connections/{connection_id}/resources")
    async def list_resources(connection_id: str, request: Request) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        try:
            resources = await _service_or_error(service).discover_resources(connection_id)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.NOT_FOUND) from exc
        return {
            "resources": [
                {
                    "id": item.id,
                    "name": item.name,
                    "resource_type": item.resource_type,
                    "source_url": item.source_url,
                    "org_shared": item.org_shared,
                    "metadata": item.metadata,
                }
                for item in resources
            ]
        }

    @router.post("/company-brain/connections/{connection_id}/preview")
    async def preview(
        connection_id: str, request: Request, body: PreviewRequest
    ) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        documents = await _service_or_error(service).preview_resources(
            connection_id,
            tuple(item.to_entity() for item in body.resources),
            limit=5,
        )
        return {
            "documents": [
                {
                    "provider": item.provider,
                    "title": item.title,
                    "markdown": item.markdown,
                    "canonical_source_url": item.canonical_source_url,
                    "content_sha256": item.content_sha256,
                    "transform_schema_version": item.transform_schema_version,
                    "visibility_class": item.visibility_class,
                }
                for item in documents
            ]
        }

    @router.post("/company-brain/connections/{connection_id}/activate")
    async def activate(
        connection_id: str, request: Request, body: ActivateRequest
    ) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        sync_service = _service_or_error(service)
        connection = await asyncio.to_thread(store.get_connection, connection_id)
        if connection is None:
            raise OmnigentError("Connection not found", code=ErrorCode.NOT_FOUND)
        if body.rrule:
            try:
                validate_rrule(body.rrule)
            except RRuleValidationError as exc:
                raise OmnigentError(
                    f"Invalid schedule: {exc}", code=ErrorCode.INVALID_INPUT
                ) from exc
        selections = []
        for resource in body.resources:
            selection = await asyncio.to_thread(
                store.create_selection,
                uuid.uuid4().hex,
                connection_id=connection.id,
                external_resource_id=resource.id,
                resource_name=resource.name,
                resource_type=resource.resource_type,
                source_url=resource.source_url,
                transform_profile={
                    "google": "google-workspace.v1",
                    "slack": "slack-thread.v1",
                    "notion": "notion-page.v1",
                }[connection.provider],
                rrule=body.rrule,
                timezone=body.timezone,
            )
            selections.append(selection)
            scheduler = getattr(request.app.state, "company_brain_scheduler", None)
            if scheduler is not None:
                scheduler.add(selection)
        runs = [
            await sync_service.sync_selection(selection.id, trigger="manual")
            for selection in selections
        ]
        return {
            "selections": [_selection_response(item) for item in selections],
            "runs": [_run_response(item) for item in runs],
        }

    @router.patch("/company-brain/selections/{selection_id}")
    async def update_selection(
        selection_id: str,
        request: Request,
        body: UpdateSelectionRequest,
    ) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        if body.rrule:
            try:
                validate_rrule(body.rrule)
            except RRuleValidationError as exc:
                raise OmnigentError(
                    f"Invalid schedule: {exc}", code=ErrorCode.INVALID_INPUT
                ) from exc
        update_arguments: dict[str, Any] = {
            "state": body.state,
            "timezone": body.timezone,
        }
        if "rrule" in body.model_fields_set:
            update_arguments["rrule"] = body.rrule
        selection = await asyncio.to_thread(
            store.update_selection,
            selection_id,
            **update_arguments,
        )
        if selection is None:
            raise OmnigentError("Selection not found", code=ErrorCode.NOT_FOUND)
        scheduler = getattr(request.app.state, "company_brain_scheduler", None)
        if scheduler is not None:
            scheduler.update(selection)
        return _selection_response(selection)

    @router.post("/company-brain/selections/{selection_id}/sync")
    async def sync_selection(selection_id: str, request: Request) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        try:
            run = await _service_or_error(service).sync_selection(selection_id, trigger="manual")
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.NOT_FOUND) from exc
        return _run_response(run)

    @router.post("/company-brain/selections/{selection_id}/retry")
    async def retry_selection(selection_id: str, request: Request) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        run = await _service_or_error(service).sync_selection(selection_id, trigger="retry")
        return _run_response(run)

    @router.delete("/company-brain/connections/{connection_id}")
    async def disconnect(connection_id: str, request: Request) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        connection = await asyncio.to_thread(store.disconnect_connection, connection_id)
        if connection is None:
            raise OmnigentError("Connection not found", code=ErrorCode.NOT_FOUND)
        selections = await asyncio.to_thread(store.list_selections, connection.id)
        for selection in selections:
            updated = await asyncio.to_thread(
                store.update_selection,
                selection.id,
                state="disconnected",
            )
            scheduler = getattr(request.app.state, "company_brain_scheduler", None)
            if scheduler is not None and updated is not None:
                scheduler.remove(updated.id)
        return _connection_response(connection)

    return router
