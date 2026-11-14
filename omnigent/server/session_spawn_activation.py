"""Revalidate a prepared child before its durable execution fence is released."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy.orm import Session
from starlette.requests import Request

from omnigent.db.account_authority import account_authority_scope
from omnigent.entities import StoredFile
from omnigent.entities.conversation import Conversation, NewConversationItem, ResourceEventData
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.startup_inputs import STARTUP_LABEL
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.session_spawn_store import (
    MAX_FILES,
    SpawnActivationCheck,
    SpawnConflictError,
    SpawnIdentity,
    SpawnReceipt,
    spawn_file_fingerprint,
)
from omnigent.telemetry.surface import SESSION_SURFACES


@dataclass
class SessionSpawnActivator:
    """Reuse normal create validation with the saved owner, never replay credentials."""

    app: Any
    conversations: SqlAlchemyConversationStore
    agents: AgentStore
    agent_cache: AgentCache
    permissions: PermissionStore | None
    files: FileStore
    artifacts: ArtifactStore
    router: RunnerRouter | None = None
    projects: ProjectStore | None = None

    def check_parent_access(self, session: Session, identity: SpawnIdentity) -> bool:
        """Read inherited edit authority under the admission/activation transaction."""
        from omnigent.server.auth import LEVEL_EDIT
        from omnigent.server.permissions import resolved_allows
        from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

        if self.permissions is None:
            return True
        if not isinstance(self.permissions, SqlAlchemyPermissionStore):
            return False
        seen: set[str] = set()
        current: str | None = identity.parent_id
        while current and current not in seen and len(seen) <= 32:
            seen.add(current)
            parent = self.conversations.get_runner_scope(current, _metadata_session=session)
            if parent is None:
                return False
            if parent.parent_conversation_id is None:
                return resolved_allows(
                    self.permissions.resolve_access(
                        identity.owner_id, current, _metadata_session=session
                    ),
                    LEVEL_EDIT,
                )
            current = parent.parent_conversation_id
        return False

    async def __call__(
        self, receipt: SpawnReceipt, conversation: Conversation
    ) -> SpawnActivationCheck:
        from omnigent.server.routes._sessions import orchestration
        from omnigent.server.routes._sessions.common import session_stream
        from omnigent.server.routes._sessions.helpers import _stored_file_to_resource
        from omnigent.server.schemas import SessionCreateRequest

        payload = receipt.create_payload
        if payload is None or not isinstance(payload.get("request"), dict):
            raise SpawnConflictError("Spawn lacks its validated creation request")
        surface = payload.get("client_surface")
        if not isinstance(surface, str) or surface not in SESSION_SURFACES | {"unknown"}:
            raise SpawnConflictError("Spawn has invalid client attribution")
        # Only normalized UI attribution is replayed. The saved account generation
        # and explicit owner argument supply authority, never these headers.
        request = Request(
            {
                "type": "http",
                "app": self.app,
                "headers": [(b"x-omnigent-client", surface.encode())],
            }
        )
        with account_authority_scope(
            receipt.identity.owner_id, receipt.identity.account_generation
        ):
            prepared = await orchestration._create_session_from_existing_agent(
                self.conversations,
                self.agents,
                self.router,
                SessionCreateRequest.model_validate(payload["request"]),
                request,
                agent_cache=self.agent_cache,
                user_id=receipt.identity.owner_id,
                permission_store=self.permissions,
                file_store=self.files,
                artifact_store=self.artifacts,
                project_store=self.projects,
                _prepare_named_child=True,
            )
            if not isinstance(
                prepared, orchestration.PreparedNamedChild
            ) or prepared.to_payload() != {
                key: value for key, value in payload.items() if key != "cost_budget"
            }:
                raise SpawnConflictError("Child configuration changed after preparation")
            for key, value in prepared.create_arguments.items():
                if key == "labels":
                    if any(
                        conversation.labels.get(label) != expected
                        for label, expected in (value or {}).items()
                        if label != STARTUP_LABEL
                    ):
                        raise SpawnConflictError("Child labels changed before activation")
                elif getattr(conversation, key) != value:
                    raise SpawnConflictError("Child binding changed before activation")
            if conversation.runner_id != receipt.identity.runner_id:
                raise SpawnConflictError("Child does not share its authenticated creator runner")

            page = await asyncio.to_thread(
                self.files.list, session_id=receipt.child_id, limit=MAX_FILES + 1, order="asc"
            )
            files = page.data
            if page.has_more or {file.id for file in files} != set(receipt.file_mapping.values()):
                raise SpawnConflictError("Child has unexpected staged attachments")
            await validate_spawn_attachments(
                self.app, conversation, files, self.files, self.artifacts, self.router
            )

            if prepared.sandbox_profile is not None:
                catalog = getattr(self.app.state, "sandbox_profiles", None)
                if catalog is None:
                    raise SpawnConflictError("Saved sandbox profile is unavailable")
                profile = await asyncio.to_thread(
                    catalog.validate_snapshot,
                    prepared.sandbox_profile,
                    user_id=receipt.identity.owner_id,
                )
                await asyncio.to_thread(
                    self.conversations.accept_sandbox_profile,
                    receipt.child_id,
                    profile,
                    _spawn_claim=receipt,
                )
            binding = await asyncio.to_thread(
                self.conversations.get_profile_host_binding, receipt.child_id
            )
            from omnigent.stores.sandbox_profile_store import encode_snapshot

            expected_profile = (
                encode_snapshot(prepared.sandbox_profile)
                if prepared.sandbox_profile is not None
                else None
            )
            if binding.profile_snapshot != expected_profile:
                raise SpawnConflictError("Child sandbox profile changed during activation")
            if self.permissions is not None:
                await asyncio.to_thread(
                    self.permissions.grant,
                    receipt.identity.owner_id,
                    receipt.child_id,
                    LEVEL_OWNER,
                    ensure_minimum=True,
                )
            for file in files:
                resource = _stored_file_to_resource(receipt.child_id, file)
                item = NewConversationItem(
                    stable_id=uuid5(UUID(receipt.child_id), f"spawn-file:{file.id}").hex,
                    type="resource_event",
                    response_id=receipt.child_id,
                    created_by=receipt.identity.owner_id,
                    data=ResourceEventData(
                        event_type="session.resource.created",
                        resource_id=file.id,
                        resource_type="file",
                        resource=resource,
                    ),
                )
                await asyncio.to_thread(self.conversations.append, receipt.child_id, [item])
                session_stream.publish(
                    receipt.child_id, {"type": "session.resource.created", "resource": resource}
                )
            cost_budget = payload.get("cost_budget")
            validate_spawn_cost_budget(cost_budget)
            return SpawnActivationCheck(
                receipt.identity.runner_id,
                conversation.host_id,
                conversation.workspace,
                {file.id: spawn_file_fingerprint(file) for file in files},
                LEVEL_OWNER if self.permissions is not None else None,
                cost_budget,
            )


async def validate_spawn_attachments(
    app: Any,
    conversation: Conversation,
    files: list[StoredFile],
    file_store: FileStore,
    artifacts: ArtifactStore,
    router: RunnerRouter | None,
) -> None:
    """Apply the normal attachment limits before reservation and again at activation."""
    from omnigent.inner.native_attachments import (
        filesystem_attachment_capability,
        uses_filesystem_delivery,
    )
    from omnigent.server.routes._sessions.helpers import (
        _enforce_filesystem_attachment_policy,
        _require_filesystem_attachment_harness,
        require_filesystem_attachment_runtime,
    )
    from omnigent.server.server_config import copy_file_count_limit, copy_total_bytes_limit

    if (
        len(files) > min(MAX_FILES, copy_file_count_limit())
        or sum(file.bytes for file in files) > copy_total_bytes_limit()
    ):
        raise SpawnConflictError("Attachments exceed the current copy limits")
    filesystem_files = [
        file for file in files if uses_filesystem_delivery(file.filename, file.source_metadata)
    ]
    if filesystem_files:
        await _require_filesystem_attachment_harness(conversation, filesystem_files[0].filename)
        await asyncio.to_thread(
            require_filesystem_attachment_runtime,
            host_id=conversation.host_id,
            runner_id=conversation.runner_id,
            host_registry=getattr(app.state, "host_registry", None),
            tunnel_registry=getattr(app.state, "tunnel_registry", None),
            runner_router=router,
            capability=filesystem_attachment_capability(
                file.filename for file in filesystem_files
            ),
        )
        _enforce_filesystem_attachment_policy(
            [file.filename for file in filesystem_files],
            session_id=None,
            file_store=file_store,
            sizes=[file.bytes for file in filesystem_files],
        )
    for file in files:
        if not await asyncio.to_thread(artifacts.exists, file.blob_key or file.id):
            raise SpawnConflictError("Attachment content is unavailable")


def validate_spawn_cost_budget(budget: Any) -> None:
    """Only the existing registered subtree-cost factory can be attached at activation."""
    from omnigent.policies.builtins.cost import subagent_cost_budget
    from omnigent.policies.registry import is_registered_handler, validate_factory_params

    if budget is None:
        return
    handler = "omnigent.policies.builtins.cost.subagent_cost_budget"
    if not isinstance(budget, dict) or not is_registered_handler(handler):
        raise SpawnConflictError("Child cost policy is unavailable")
    error = validate_factory_params(handler, budget)
    if error:
        raise SpawnConflictError(error)
    try:
        subagent_cost_budget(**budget)
    except (TypeError, ValueError) as exc:
        raise SpawnConflictError("Invalid child cost budget") from exc
