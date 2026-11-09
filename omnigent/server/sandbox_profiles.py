"""Provider discovery and immutable sandbox profile references."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.profiles import ProviderSandboxProfile
from omnigent.server.managed_hosts import ManagedSandboxDeployment, build_owner_launcher
from omnigent.server.provider_sandbox_profiles import (
    ProviderProfileSnapshot,
    profile_context,
    provider_call,
    select_provider_profile,
)
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.sandbox_profile_store import (
    SandboxProfileStore,
    encode_snapshot,
    same_profile_identity,
)

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.host_store import HostStore


def _invalid_reference(error: ValueError) -> HTTPException:
    if isinstance(error, ValidationError):
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_input=False)
        )
    else:
        detail = str(error)
    return HTTPException(status_code=422, detail=detail)


class SandboxProfileCatalog:
    """Resolve current permission to use a provider-owned profile at its saved revision."""

    def __init__(
        self,
        conversations: ConversationStore,
        deployment: ManagedSandboxDeployment | None,
        host_store: HostStore | None = None,
    ) -> None:
        self._store = SandboxProfileStore(conversations)
        self._deployment = deployment
        self._host_store = host_store

    def list_profiles(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        provider: str | None = None,
        user_id: str | None = None,
        search: str = "",
        profile_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        managed = self.managed_providers(provider)
        if not 1 <= limit <= 100:
            raise HTTPException(
                status_code=422, detail="Profile page size must be between 1 and 100"
            )
        after: tuple[str, str] | None = None
        if cursor is not None:
            try:
                parts = json.loads(cursor)
                if (
                    not isinstance(parts, list)
                    or len(parts) != 2
                    or not all(isinstance(p, str) for p in parts)
                ):
                    raise ValueError("Invalid cursor")
                after = (parts[0], parts[1])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=422, detail="Invalid sandbox profile cursor"
                ) from None
        rows = []
        for provider in managed:
            rows.extend(self._provider_profiles(provider, user_id))
        if profile_id is not None:
            rows = [row for row in rows if row["id"] == profile_id]
        if search.strip():
            query = search.strip().casefold()
            rows = [
                row
                for row in rows
                if any(query in row[key].casefold() for key in ("id", "name", "description"))
            ]
        rows.sort(key=lambda row: (row["provider"], row["id"]))
        rows = [row for row in rows if after is None or (row["provider"], row["id"]) > after]
        page = rows[:limit]
        next_cursor = (
            json.dumps([page[-1]["provider"], page[-1]["id"]]) if len(rows) > limit else None
        )
        return page, next_cursor

    def managed_providers(self, provider: str | None = None) -> list[str]:
        if self._deployment is None:
            return []
        managed = []
        for entry in self._deployment.configs:
            if provider is not None and entry.provider != provider:
                continue
            if not entry.managed_launch_supported:
                continue
            if not entry.manages_profiles:
                continue
            if not entry.provider or not entry.provider.strip():
                raise HTTPException(
                    status_code=422,
                    detail="Provider-managed sandbox profiles require a configured provider name",
                )
            managed.append(entry.provider)
        return managed

    def _provider_profiles(
        self, provider: str, user_id: str | None, *, sandbox_id: str | None = None
    ) -> list[dict[str, Any]]:
        context = profile_context(user_id, sandbox_id)
        assert self._deployment is not None
        entry = self._deployment.for_provider(provider)
        assert entry is not None

        def read() -> list[ProviderSandboxProfile]:
            profiles = TypeAdapter(list[ProviderSandboxProfile]).validate_python(
                build_owner_launcher(entry, self._deployment, context.user_id).list_profiles(
                    context
                )
            )
            if len(profiles) > 1000 or len({p.id for p in profiles}) != len(profiles):
                raise ValueError("Provider catalog must have at most 1000 unique profiles")
            return profiles

        return [
            {
                **profile.model_dump(),
                "provider": provider,
            }
            for profile in provider_call(read, allow_gone=sandbox_id is not None)
        ]

    def require_selection(self, provider: str | None, *, user_id: str | None = None) -> None:
        if self._deployment is None:
            return
        provider = provider or self._deployment.default.provider
        if provider in self.managed_providers(provider):
            profile_context(user_id)
            raise HTTPException(
                status_code=422, detail="Choose a sandbox profile for this provider"
            )

    def resolve(
        self,
        profile_id: str,
        expected_revision: str | None = None,
        provider: str | None = None,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        selected_provider = provider or (
            self._deployment.default.provider if self._deployment else None
        )
        if selected_provider in self.managed_providers(selected_provider):
            assert selected_provider is not None
            context = profile_context(user_id)
            profiles = self._provider_profiles(selected_provider, user_id)
            selected = next((p for p in profiles if p["id"] == profile_id), None)
            if selected is None:
                raise HTTPException(status_code=403, detail="Sandbox profile access denied")
            if expected_revision is not None and selected["revision"] != expected_revision:
                raise HTTPException(
                    status_code=409, detail="Sandbox profile changed. Choose a profile again."
                )
            snapshot = ProviderProfileSnapshot.model_validate(
                {
                    **selected,
                    "user_id": context.user_id,
                    "workspace_id": context.workspace_id,
                }
            ).model_dump()
            return self.validate_snapshot(snapshot, user_id=context.user_id)
        raise HTTPException(
            status_code=422, detail="Sandbox profiles are unavailable for this provider"
        )

    def validate_snapshot(
        self, snapshot: dict[str, Any], *, user_id: str | None, sandbox_id: str | None = None
    ) -> dict[str, Any]:
        """Recheck authorization for a session's accepted provider reference."""
        try:
            saved_reference = ProviderProfileSnapshot.model_validate(snapshot).model_dump()
            if saved_reference["user_id"] != profile_context(user_id).user_id:
                raise HTTPException(
                    status_code=403, detail="Sandbox profile execution identity mismatch"
                )
            if (
                saved_reference["workspace_id"]
                != profile_context(saved_reference["user_id"]).workspace_id
            ):
                raise HTTPException(
                    status_code=403, detail="Sandbox profile belongs to another workspace"
                )
            encode_snapshot(saved_reference)
        except ValueError as exc:
            raise _invalid_reference(exc) from exc
        entry = (
            self._deployment.for_provider(saved_reference["provider"])
            if self._deployment
            else None
        )
        if entry is None or not entry.managed_launch_supported:
            raise HTTPException(status_code=409, detail="Sandbox profile provider unavailable")
        launcher = provider_call(
            lambda: build_owner_launcher(entry, self._deployment, saved_reference["user_id"])
        )
        select_provider_profile(launcher, saved_reference, sandbox_id=sandbox_id)
        return saved_reference

    def inherit_snapshot(self, snapshot: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        context = profile_context(user_id)
        return self.validate_snapshot({**snapshot, "user_id": context.user_id}, user_id=user_id)

    def bind_session(self, session_id: str, snapshot: dict[str, Any]) -> None:
        try:
            self._store.bind_session(
                session_id, self.validate_snapshot(snapshot, user_id=snapshot["user_id"])
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def get_session_snapshot(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self._store.get_session_snapshot(session_id)
        except ValueError as exc:
            raise _invalid_reference(exc) from exc

    def admit_runner_connection(self, runner_id: str) -> bool:
        """Fence delegated reconnects against stale profiled-runner replacement."""
        if not self.managed_providers():
            return True
        from omnigent.db.utils import now_epoch

        conversations = self._store.conversations
        conversations.touch_profile_runner_liveness(runner_id, now_epoch())
        return bool(conversations.get_runner_host_ids(runner_id))

    def reassign_runner(
        self,
        conversation: Conversation,
        runner_id: str | None,
        *,
        conversation_store: ConversationStore,
        host_store: HostStore | None,
        user_id: str | None,
    ) -> bool:
        """Authorize a rotation and retain the runner's trusted host and workspace."""
        if runner_id is not None and runner_id == conversation.runner_id:
            snapshot = self.get_session_snapshot(conversation.id)
            if snapshot is not None:
                self.validate_snapshot(snapshot, user_id=user_id)
            return False
        current_host_id = conversation.host_id
        if conversation.runner_id and current_host_id:
            current_host = host_store.get_host(current_host_id) if host_store else None
            protected = self.get_host_snapshot(
                current_host_id, reference_session_id=conversation.id
            ) is not None or (
                current_host is not None
                and current_host.sandbox_provider is not None
                and current_host.sandbox_provider
                in self.managed_providers(current_host.sandbox_provider)
            )
            if protected:
                if runner_id is not None:
                    raise OmnigentError(
                        "A managed session cannot change to a different runner",
                        code=ErrorCode.INVALID_INPUT,
                    )
                self._rotate_runner(conversation_store, conversation, current_host_id, None)
                return True
        if runner_id is None:
            try:
                conversation_store.clear_runner_id(conversation.id, expected_binding=conversation)
            except ValueError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            return True
        profile = self.get_session_snapshot(conversation.id)
        if profile is None:
            try:
                if not self.managed_providers():
                    return False
            except HTTPException:
                # Invalid profile configuration still requires host-specific checks below.
                pass
        host_ids = conversation_store.get_runner_host_ids(runner_id)
        if len(host_ids) > 1:
            raise OmnigentError(
                "The runner is associated with multiple hosts; launch a new runner",
                code=ErrorCode.INVALID_INPUT,
            )
        if not host_ids:
            if profile is not None:
                raise OmnigentError(
                    "Launch this session on a managed host with its saved sandbox profile",
                    code=ErrorCode.INVALID_INPUT,
                )
            return False
        host_id = host_ids[0]
        host = host_store.get_host(host_id) if host_store else None
        if host is None:
            raise OmnigentError(
                "The runner's host is no longer available", code=ErrorCode.INVALID_INPUT
            )
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="Host owner mismatch")
        side_chat = conversation.labels.get(SIDE_CHAT_LABEL_KEY) == "1"
        if side_chat and profile is not None:
            source_id = conversation.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
            source_runners = conversation_store.get_runner_ids([source_id]) if source_id else {}
            if source_id is None or source_runners.get(source_id) != runner_id:
                raise OmnigentError(
                    "A side chat must use its source session's runner",
                    code=ErrorCode.INVALID_INPUT,
                )
        self.require_session_host(
            conversation.id,
            host_id,
            bind=False,
            sandbox_provider=host.sandbox_provider,
            user_id=host.user_id,
        )
        host_profile = self.get_host_snapshot(host_id)
        if host_profile is None:
            return False
        if profile is None and conversation.runner_id is not None:
            raise OmnigentError(
                "Detach this session's runner before adopting a sandbox profile",
                code=ErrorCode.INVALID_INPUT,
            )
        if current_host_id is not None and current_host_id != host_id:
            raise HTTPException(status_code=409, detail="Session and runner hosts do not match")
        self._rotate_runner(
            conversation_store,
            conversation,
            host_id,
            runner_id,
            keep_hostless=side_chat and current_host_id is None,
            profile_snapshot=profile if profile is not None else host_profile,
        )
        return True

    @staticmethod
    def _rotate_runner(
        conversation_store: ConversationStore,
        conversation: Conversation,
        host_id: str,
        runner_id: str | None,
        *,
        keep_hostless: bool = False,
        profile_snapshot: dict[str, Any] | None = None,
    ) -> None:
        try:
            conversation_store.reassign_runner_with_host(
                conversation.id,
                host_id,
                runner_id,
                expected_runner_id=conversation.runner_id,
                keep_hostless=keep_hostless,
                profile_snapshot=profile_snapshot,
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

    def _saved_host_snapshot(
        self, host_id: str, reference_session_id: str | None = None
    ) -> dict[str, Any] | None:
        try:
            return self._store.get_host_snapshot(
                host_id, reference_session_id=reference_session_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def get_host_snapshot(
        self, host_id: str, *, reference_session_id: str | None = None
    ) -> dict[str, Any] | None:
        host = self._host_store.get_host(host_id) if self._host_store else None
        if (
            host is None
            or not host.sandbox_provider
            or host.sandbox_provider not in self.managed_providers(host.sandbox_provider)
        ):
            return None
        if (
            host is not None
            and host.deleted_at is None
            and host.sandbox_provider
            and host.sandbox_id
            and self._deployment
        ):
            entry = self._deployment.for_provider(host.sandbox_provider)
            if entry is not None:
                launcher = provider_call(entry.launcher_factory)
                if launcher.manages_profiles and launcher.supports_sandbox_profile_lookup:
                    try:
                        rows = self._provider_profiles(
                            host.sandbox_provider, host.user_id, sandbox_id=host.sandbox_id
                        )
                    except SandboxGoneError:
                        # Surviving sessions retain the exact revision for replacement.
                        saved = self._saved_host_snapshot(host_id, reference_session_id)
                        if saved is not None:
                            return self.validate_snapshot(saved, user_id=host.user_id)
                        raise HTTPException(
                            status_code=409, detail="Sandbox no longer exists"
                        ) from None
                    if len(rows) != 1:
                        raise HTTPException(
                            status_code=409,
                            detail="The controller did not identify this sandbox's exact profile",
                        )
                    snapshot = ProviderProfileSnapshot.model_validate(
                        {
                            **rows[0],
                            "user_id": host.user_id,
                            "workspace_id": profile_context(host.user_id).workspace_id,
                        }
                    ).model_dump()
                    try:
                        self.validate_snapshot(
                            snapshot, user_id=host.user_id, sandbox_id=host.sandbox_id
                        )
                    except SandboxGoneError:
                        raise HTTPException(
                            status_code=409, detail="Sandbox disappeared; retry the request"
                        ) from None
                    assert self._host_store is not None
                    current = self._host_store.get_host(host_id)
                    if (
                        current is None
                        or current.deleted_at is not None
                        or (current.sandbox_id, current.sandbox_provider, current.user_id)
                        != (host.sandbox_id, host.sandbox_provider, host.user_id)
                    ):
                        raise HTTPException(
                            status_code=409, detail="The sandbox changed; retry the request"
                        )
                    return snapshot
        saved = self._saved_host_snapshot(host_id)
        if saved is not None and saved["user_id"] != host.user_id:
            raise HTTPException(
                status_code=403, detail="Sandbox profile execution identity mismatch"
            )
        return saved

    def require_session_host(
        self,
        session_id: str,
        host_id: str,
        *,
        bind: bool = True,
        sandbox_provider: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """After host ownership checks, inherit or match the host's accepted profile."""
        host = self._host_store.get_host(host_id) if self._host_store else None
        if host is not None:
            if user_id is not None and host.user_id != user_id:
                raise HTTPException(status_code=403, detail="Host owner mismatch")
            user_id = host.user_id
        session_snapshot = self.get_session_snapshot(session_id)
        if session_snapshot is not None and session_snapshot["user_id"] != user_id:
            raise HTTPException(
                status_code=403, detail="Sandbox profile execution identity mismatch"
            )
        host_snapshot = self.get_host_snapshot(host_id, reference_session_id=session_id)
        if host_snapshot is not None:
            self.validate_snapshot(host_snapshot, user_id=user_id)
        elif sandbox_provider is not None:
            self.require_selection(sandbox_provider, user_id=user_id)
        if (
            session_snapshot is not None or host_snapshot is not None
        ) and not self._store.can_bind_host(session_id, host_id):
            raise HTTPException(
                status_code=409, detail="A profiled session cannot be moved to a different host"
            )
        if session_snapshot is not None and (
            host_snapshot is None or not same_profile_identity(session_snapshot, host_snapshot)
        ):
            raise HTTPException(
                status_code=409, detail="Session and host sandbox profiles do not match"
            )
        if not bind:
            return
        if session_snapshot is None and host_snapshot is not None:
            conversations = self._store.conversations
            binding = conversations.get_profile_host_binding(session_id)
            if binding.host_id != (host.host_id if host else host_id) or binding.workspace is None:
                raise HTTPException(status_code=409, detail="The session binding changed; retry")
            snapshot = self.validate_snapshot(host_snapshot, user_id=user_id)
            try:
                conversations.bind_profiled_host(
                    session_id,
                    host_id,
                    binding.workspace,
                    snapshot,
                    git_branch=binding.git_branch,
                    expected_binding=binding,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
