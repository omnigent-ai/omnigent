"""Managed-sandbox model previews and saved session-policy validation."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inference_config import (
    binding_for_harness,
    parse_inference_config,
    resolve_bound_provider,
)
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access_and_level, require_user


def inference_service(request: Request) -> Any:
    """Use the service owned by this server's application state."""
    from omnigent.server.inference_catalog import SandboxInferenceService

    service = getattr(request.app.state, "inference_catalog", None)
    if service is None:
        service = SandboxInferenceService(request.app.state)
        request.app.state.inference_catalog = service
    return service


def actual_harness(spec: Any, override: str | None = None) -> str:
    """Preserve ACP slugs and native identities when selecting a profile binding."""
    declared = spec.executor.config.get("harness") or spec.executor.type
    if override == "acp" and declared.startswith("acp:"):
        return declared
    return override or declared


def configured_snapshot(snapshot: dict[str, Any] | None) -> bool:
    """Whether the session's own harness has an inference binding."""
    return (
        snapshot is not None
        and binding_for_harness(snapshot["runtime_config"], snapshot["harness"]) is not None
    )


def managed_inference_configured(request: Request, provider: str | None) -> bool:
    """Whether a managed target requires an identifiable harness before creation."""
    deployment = getattr(request.app.state, "sandbox_config", None)
    target = deployment.for_provider(provider) if deployment is not None else None
    return bool(target and parse_inference_config(target.host_config or {}))


def selected_catalog_model(catalog: dict[str, Any], model: str | None) -> str:
    """Reject unavailable selections before persistence or prompt dispatch."""
    if catalog.get("status") != "ready":
        raise OmnigentError(
            catalog.get("error") or "No available models match this harness's configuration",
            code=ErrorCode.INVALID_INPUT,
        )
    selected = model or catalog.get("default_model")
    if not selected or selected not in {row["id"] for row in catalog["models"]}:
        raise OmnigentError(
            f"Model {selected!r} is not available in this session's configured model list",
            code=ErrorCode.INVALID_INPUT,
        )
    return selected


async def prepare_create_inference(
    request: Request,
    body: Any,
    spec: Any,
    user_id: str | None,
    conversation_store: Any,
    *,
    harness_override: str | None = None,
    model_override: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Capture the accepted configuration before a session or sandbox exists."""
    harness = actual_harness(spec, harness_override)
    if (
        harness == "auto"
        and body.host_type == "managed"
        and managed_inference_configured(request, body.sandbox_provider)
    ):
        raise OmnigentError(
            "Choose a harness to use this sandbox's configured inference providers",
            code=ErrorCode.INVALID_INPUT,
        )
    snapshot = None
    if body.parent_session_id:
        parent = await asyncio.to_thread(
            conversation_store.get_conversation, body.parent_session_id
        )
        if parent is not None and parent.inference_snapshot is not None:
            snapshot = copy.deepcopy(parent.inference_snapshot)
            snapshot["harness"] = harness
    if snapshot is None and body.host_type == "managed":
        snapshot = await inference_service(request).prepare(
            body.sandbox_provider, harness, user_id, agent_auth=spec.executor.auth
        )
        revision = body.inference_configuration_revision
        if revision is not None and (
            snapshot is None or revision != snapshot["configuration_revision"]
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "inference_configuration_changed",
                    "message": "Harness configuration changed. Refresh the model choices.",
                },
            )
    if snapshot is not None and harness == "auto":
        raise OmnigentError(
            "Choose a harness to use the parent session's saved inference providers",
            code=ErrorCode.INVALID_INPUT,
        )
    if not configured_snapshot(snapshot):
        return snapshot, model_override
    assert snapshot is not None
    resolve_bound_provider(snapshot["runtime_config"], harness, spec.executor.auth)
    if spec.executor.profile:
        raise OmnigentError(
            "The agent's Databricks profile conflicts with the configured harness provider",
            code=ErrorCode.INVALID_INPUT,
        )
    if getattr(body, "cost_control_mode_override", None) == "on" or harness_override == "auto":
        raise OmnigentError(
            "Automatic provider routing is unavailable for a bound inference profile",
            code=ErrorCode.INVALID_INPUT,
        )
    catalog = await inference_service(request).catalog(snapshot)
    snapshot["catalog"] = catalog
    selected = selected_catalog_model(catalog, model_override)
    return snapshot, selected


async def validate_saved_selection(
    request: Request, conversation: Conversation, model: str | None
) -> str | None:
    """Recheck availability using saved provider/credential identity, never current defaults."""
    snapshot = conversation.inference_snapshot
    if not configured_snapshot(snapshot):
        return model
    catalog = await inference_service(request).catalog(snapshot)
    return selected_catalog_model(catalog, model)


def create_sandbox_inference_router(
    *,
    agent_store: Any,
    agent_cache: Any,
    conversation_store: Any,
    permission_store: Any,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    """Expose authenticated previews without provisioning a host."""
    router = APIRouter()

    @router.get("/sandbox-providers/{provider}/harnesses/{harness}/model-options")
    async def model_options(
        request: Request, provider: str, harness: str, agent_id: str | None = None
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        auth = None
        if agent_id is not None:
            agent = await asyncio.to_thread(agent_store.get, agent_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="Agent not found")
            if agent.session_id:
                await require_access_and_level(
                    user_id, agent.session_id, LEVEL_READ, permission_store, conversation_store
                )
            spec = (
                await asyncio.to_thread(
                    agent_cache.load,
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.session_id is None,
                )
            ).spec
            harness = actual_harness(spec, harness)
            auth = spec.executor.auth
        try:
            snapshot = await inference_service(request).prepare(
                provider, harness, user_id, agent_auth=auth
            )
            if snapshot is not None:
                return snapshot["catalog"]
        except (OmnigentError, ValueError) as exc:
            return {
                "configured": True,
                "models": [],
                "configuration_revision": None,
                "provider_label": None,
                "default_model": None,
                "status": "unavailable",
                "error": exc.message
                if isinstance(exc, OmnigentError)
                else "Invalid inference configuration",
            }
        return {
            "configured": False,
            "models": [],
            "configuration_revision": None,
            "provider_label": None,
            "default_model": None,
            "status": "unconfigured",
        }

    return router
