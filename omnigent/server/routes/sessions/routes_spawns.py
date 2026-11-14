"""Runner-scoped reservation and admission of named child sessions."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.account_authority import account_generation
from omnigent.entities.conversation import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    LEVEL_EDIT,
    AuthProvider,
    require_runner_session_scope,
    runner_token_confinement,
)
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._sessions.orchestration import (
    PreparedNamedChild,
    _create_session_from_existing_agent,
)
from omnigent.server.schemas import SessionCreateRequest, SessionStartupInput
from omnigent.server.session_spawn_activation import (
    SessionSpawnActivator,
    validate_spawn_attachments,
    validate_spawn_cost_budget,
)
from omnigent.server.session_spawns import SessionSpawnCoordinator
from omnigent.server.startup_inputs import prepare_startup_input
from omnigent.stores.file_store import FileStore
from omnigent.stores.session_spawn_store import (
    MAX_FILES,
    SpawnAuthorityError,
    SpawnConflictError,
    SpawnIdentity,
    SpawnNameConflictError,
    SpawnReceipt,
    spawn_file_fingerprint,
)

Epoch = Annotated[int, Field(strict=True, ge=1, lt=2**63)]


class PrepareSpawnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epoch: Epoch
    nonce: UUID
    create: SessionCreateRequest
    file_ids: list[UUID] = Field(default_factory=list, max_length=MAX_FILES)
    cost_budget: dict[str, Any] | None = None


class AdmitSpawnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epoch: Epoch
    input: SessionStartupInput


class AbortSpawnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epoch: Epoch


@contextmanager
def _spawn_errors():
    try:
        yield
    except SpawnAuthorityError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.RUNNER_SCOPE_DENIED) from exc
    except SpawnConflictError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc


async def _response(
    receipt: SpawnReceipt | None, *, epoch: int, nonce: str, file_store: FileStore
) -> dict[str, Any]:
    if receipt is None:
        return {"epoch": epoch, "nonce": nonce, "state": "unknown"}
    files = {}
    for source, target in receipt.file_mapping.items():
        file = await asyncio.to_thread(file_store.get, target, session_id=receipt.child_id)
        if file is not None:
            files[source] = {
                "new_id": target,
                "filename": file.filename,
                "content_type": file.content_type,
            }
    return {
        "epoch": receipt.epoch,
        "nonce": receipt.nonce,
        "child_id": receipt.child_id,
        "state": receipt.state.name.lower(),
        "phase": receipt.phase.name.lower(),
        "file_mapping": receipt.file_mapping,
        "files": files,
        "labels": (receipt.create_payload or {}).get("arguments", {}).get("labels", {}),
    }


def register_spawn_routes(router: APIRouter, *, auth_provider: AuthProvider | None) -> None:
    """Install only alongside the complete coordinator and runner capability contract."""

    async def context(
        request: Request, parent_id: str
    ) -> tuple[SessionSpawnCoordinator, SessionSpawnActivator, SpawnIdentity]:
        user_id = require_user(request, auth_provider)
        confinement = runner_token_confinement(request)
        if confinement is None or user_id is None:
            raise OmnigentError(
                "Pending child spawns require a scoped runner credential",
                code=ErrorCode.RUNNER_SCOPE_DENIED,
            )
        require_runner_session_scope(request, parent_id)
        coordinator = getattr(request.app.state, "session_spawns", None)
        if not isinstance(coordinator, SessionSpawnCoordinator) or not isinstance(
            coordinator.activate, SessionSpawnActivator
        ):
            raise OmnigentError("Pending child spawns are unavailable", code=ErrorCode.CONFLICT)
        activate = coordinator.activate
        await require_access(
            user_id, parent_id, LEVEL_EDIT, activate.permissions, coordinator.conversations
        )
        await asyncio.to_thread(coordinator.conversations.require_spawn_activated, parent_id)
        return (
            coordinator,
            activate,
            SpawnIdentity(confinement.runner_id, parent_id, user_id, account_generation(user_id)),
        )

    def wake(request: Request) -> None:
        recovery = getattr(request.app.state, "session_spawn_recovery", None)
        if recovery is not None:
            recovery.wake()

    @router.get("/sessions/{parent_id}/spawns/epoch")
    async def current_epoch(request: Request, parent_id: UUID) -> dict[str, int]:
        with _spawn_errors():
            coordinator, _, identity = await context(request, parent_id.hex)
            epoch = await asyncio.to_thread(coordinator.spawns.current_epoch, identity)
            return {"epoch": epoch}

    @router.post("/sessions/{parent_id}/spawns", status_code=201, response_model=None)
    async def prepare(
        request: Request, parent_id: UUID, body: PrepareSpawnRequest
    ) -> dict[str, Any] | JSONResponse:
        from omnigent.server.routes.sessions.routes_core import (
            _require_runner_scoped_agent,
            _runner_created_labels,
        )
        from omnigent.server.server_config import copy_file_count_limit, copy_total_bytes_limit

        with _spawn_errors():
            coordinator, activate, identity = await context(request, parent_id.hex)
            if body.create.parent_session_id != parent_id.hex:
                raise OmnigentError(
                    "Spawn parent must match the route", code=ErrorCode.INVALID_INPUT
                )
            if (
                len(set(body.file_ids)) != len(body.file_ids)
                or len(body.file_ids) > copy_file_count_limit()
            ):
                raise OmnigentError("Invalid spawn attachment count", code=ErrorCode.INVALID_INPUT)
            await _require_runner_scoped_agent(
                request, body.create.agent_id, identity.owner_id, activate.agents
            )
            create = body.create.model_copy(
                update={
                    "labels": _runner_created_labels(
                        body.create.labels, runner_token_confinement(request)
                    )
                }
            )
            prepared = await _create_session_from_existing_agent(
                coordinator.conversations,
                activate.agents,
                activate.router,
                create,
                request,
                agent_cache=activate.agent_cache,
                user_id=identity.owner_id,
                permission_store=activate.permissions,
                file_store=activate.files,
                artifact_store=activate.artifacts,
                project_store=activate.projects,
                _prepare_named_child=True,
            )
            if not isinstance(prepared, PreparedNamedChild):
                raise SpawnConflictError("Child preparation did not produce a reservation")
            if prepared.create_arguments.get("runner_id") != identity.runner_id:
                raise SpawnAuthorityError("Child must inherit the authenticated creator runner")
            sources = []
            for file_id in body.file_ids:
                source = await asyncio.to_thread(
                    activate.files.get, file_id.hex, session_id=parent_id.hex
                )
                if source is None:
                    raise OmnigentError(
                        "Source attachment is unavailable", code=ErrorCode.NOT_FOUND
                    )
                sources.append(source)
            arguments: dict[str, Any] = {
                **prepared.create_arguments,
                "labels": prepared.create_arguments.get("labels") or {},
            }
            preview = Conversation(
                id=uuid4().hex,
                created_at=0,
                updated_at=0,
                root_conversation_id=parent_id.hex,
                **arguments,
            )
            await validate_spawn_attachments(
                request.app, preview, sources, activate.files, activate.artifacts, activate.router
            )
            validate_spawn_cost_budget(body.cost_budget)
            create_payload = prepared.to_payload()
            if body.cost_budget is not None:
                create_payload["cost_budget"] = body.cost_budget
            if create.title is None:
                raise SpawnConflictError("Prepared child is missing its title")
            try:
                receipt = await asyncio.to_thread(
                    coordinator.spawns.reserve,
                    identity,
                    body.nonce.hex,
                    epoch=body.epoch,
                    title=create.title,
                    create_payload=create_payload,
                    source_file_ids=tuple(file_id.hex for file_id in body.file_ids),
                    max_file_bytes=copy_total_bytes_limit(),
                    source_fingerprints={
                        source.id: spawn_file_fingerprint(source) for source in sources
                    },
                )
            except SpawnNameConflictError:
                return JSONResponse(
                    status_code=409,
                    content={
                        "epoch": body.epoch,
                        "nonce": body.nonce.hex,
                        "state": "rejected",
                        "reason": "name_conflict",
                    },
                )
            return await _response(
                receipt, epoch=body.epoch, nonce=body.nonce.hex, file_store=activate.files
            )

    @router.get("/sessions/{parent_id}/spawns/{nonce}")
    async def reconcile(
        request: Request,
        parent_id: UUID,
        nonce: UUID,
        epoch: Annotated[int, Query(ge=1, lt=2**63)],
    ) -> dict[str, Any]:
        with _spawn_errors():
            coordinator, activate, identity = await context(request, parent_id.hex)
            receipt = await asyncio.to_thread(
                coordinator.spawns.get, identity, nonce.hex, epoch=epoch
            )
            return await _response(
                receipt, epoch=epoch, nonce=nonce.hex, file_store=activate.files
            )

    @router.post("/sessions/{parent_id}/spawns/{nonce}/input", status_code=202)
    async def admit(
        request: Request, parent_id: UUID, nonce: UUID, body: AdmitSpawnRequest
    ) -> dict[str, Any]:
        with _spawn_errors():
            coordinator, activate, identity = await context(request, parent_id.hex)
            receipt = await asyncio.to_thread(
                coordinator.spawns.get, identity, nonce.hex, epoch=body.epoch
            )
            if receipt is None:
                raise SpawnConflictError("Spawn attempt is unknown")
            item = prepare_startup_input(
                receipt.child_id, body.input, user_id=identity.owner_id, headers=request.headers
            )
            receipt = await asyncio.to_thread(
                coordinator.spawns.admit,
                identity,
                nonce.hex,
                item.model_dump(mode="json"),
                epoch=body.epoch,
            )
            wake(request)
            return await _response(
                receipt, epoch=body.epoch, nonce=nonce.hex, file_store=activate.files
            )

    @router.post("/sessions/{parent_id}/spawns/{nonce}/abort")
    async def abort(
        request: Request, parent_id: UUID, nonce: UUID, body: AbortSpawnRequest
    ) -> dict[str, Any]:
        with _spawn_errors():
            coordinator, activate, identity = await context(request, parent_id.hex)
            receipt = await asyncio.to_thread(
                coordinator.spawns.abort, identity, nonce.hex, epoch=body.epoch
            )
            wake(request)
            return await _response(
                receipt, epoch=body.epoch, nonce=nonce.hex, file_store=activate.files
            )
