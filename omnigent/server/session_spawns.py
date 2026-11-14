"""Recover admitted child spawns without recreating or deleting uncertain work."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.entities.conversation import Conversation, NewConversationItem
from omnigent.server.auth import runner_session_in_scope
from omnigent.server.startup_inputs import ManagedStartupInputs, initial_input_id
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_spawn_store import (
    SessionSpawnStore,
    SpawnActivationCheck,
    SpawnConflictError,
    SpawnIdentity,
    SpawnPhase,
    SpawnReceipt,
    SpawnState,
)

_logger = logging.getLogger(__name__)


class SessionSpawnCoordinator:
    """Advance a bounded outbox page using receipt-bound persistence and activation.

    Activation must restore current ownership, policy and attachment checks before
    releasing the durable execution fence.
    """

    def __init__(
        self,
        conversations: SqlAlchemyConversationStore,
        startup: ManagedStartupInputs,
        *,
        activate: Callable[[SpawnReceipt, Conversation], Awaitable[SpawnActivationCheck]],
        cleanup_blob: Callable[[str], None],
        artifact_store: ArtifactStore,
        check_current_access: Callable[[Session, SpawnIdentity], bool] | None = None,
    ) -> None:
        self.conversations = conversations
        self.startup = startup
        self.activate = activate
        self.cleanup_blob = cleanup_blob

        def scope(session: Session, runner_id: str, parent_id: str) -> bool:
            return runner_session_in_scope(
                runner_id,
                parent_id,
                lambda current: conversations.get_runner_scope(current, _metadata_session=session),
            )

        from omnigent.server.session_spawn_activation import SessionSpawnActivator

        if check_current_access is None and isinstance(activate, SessionSpawnActivator):
            check_current_access = activate.check_parent_access
        self.spawns = SessionSpawnStore(
            conversations.storage_location,
            encode_payload=lambda value: conversations._encode_item_data_batch([value])[0],
            decode_payload=lambda value: conversations._decode_item_data_batch([value])[0],
            check_current_scope=scope,
            check_current_access=check_current_access,
            artifact_store=artifact_store,
        )

    async def advance(self, app: Any, receipt: SpawnReceipt) -> None:
        """Reconcile one retained attempt; exceptions leave it available for recovery."""
        if receipt.state == SpawnState.ADMITTED and await asyncio.to_thread(
            self.conversations.spawn_was_deleted, receipt
        ):
            await asyncio.to_thread(self.spawns.retire_deleted_materialization, receipt)
            return
        if receipt.state == SpawnState.ABORTING:
            current = await asyncio.to_thread(self.spawns.get_claimed_cleanup, receipt.child_id)
        else:
            current = await asyncio.to_thread(
                self.spawns.get, receipt.identity, receipt.nonce, epoch=receipt.epoch
            )
        if current is None:
            return
        receipt = current
        if receipt.state == SpawnState.ABORTING:
            for key in receipt.cleanup_blob_keys:
                await asyncio.to_thread(self.cleanup_blob, key)
                await asyncio.to_thread(self.spawns.finish_abort, receipt.child_id, (key,))
            return
        if receipt.state != SpawnState.ADMITTED or receipt.phase == SpawnPhase.READY:
            return
        if receipt.create_payload is None or receipt.input_payload is None:
            raise SpawnConflictError("Admitted child is missing its durable payload")
        if receipt.phase == SpawnPhase.MATERIALIZE:
            arguments = receipt.create_payload.get("arguments")
            if not isinstance(arguments, dict) or any(key.startswith("_") for key in arguments):
                raise SpawnConflictError("Invalid prepared child creation arguments")
            if "conversation_id" in arguments:
                raise SpawnConflictError("Prepared child cannot select an ID")
            item = NewConversationItem.model_validate(receipt.input_payload)
            if item.stable_id != initial_input_id(receipt.child_id):
                raise SpawnConflictError("Prepared child first input has a different identity")
            await asyncio.to_thread(
                self.conversations.create_conversation,
                **arguments,
                conversation_id=receipt.child_id,
                _spawn_claim=receipt,
                _spawn_first_input=item,
            )
            await asyncio.to_thread(
                self.spawns.advance_materialization, receipt, SpawnPhase.MATERIALIZE
            )
            return
        if receipt.phase == SpawnPhase.ACTIVATE:
            conversation = await asyncio.to_thread(
                self.conversations.get_conversation, receipt.child_id
            )
            if conversation is None:
                raise SpawnConflictError("Admitted child was deleted before activation")
            checked = await self.activate(receipt, conversation)
            await asyncio.to_thread(
                self.spawns.advance_materialization,
                receipt,
                SpawnPhase.ACTIVATE,
                activation_check=checked,
            )
            if checked is not None and checked.cost_budget is not None:
                from omnigent.runtime.policies.builder import invalidate_session_policy_specs_cache

                invalidate_session_policy_specs_cache(receipt.child_id)
            return
        if receipt.phase == SpawnPhase.DISPATCH:
            item = await asyncio.to_thread(
                self.conversations.get_item, receipt.child_id, initial_input_id(receipt.child_id)
            )
            if item is not None and item.status == "in_progress":
                await self.startup.resume_session(app, receipt.child_id)
                return
            # Stop may have deleted the row; a claimed/failed input is also final
            # for this coordinator. Its normal delivery recovery owns what follows.
            await asyncio.to_thread(
                self.spawns.advance_materialization, receipt, SpawnPhase.DISPATCH
            )

    async def recover_page(
        self,
        app: Any,
        state: SpawnState,
        phase: SpawnPhase,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[str | None, list[tuple[str, Exception]]]:
        """Recover one workspace-scoped page; callers retain the returned cursor."""
        receipts = await asyncio.to_thread(
            self.spawns.list_work, state, phase, after=after, limit=limit
        )
        failures = []
        for receipt in receipts:
            try:
                await self.advance(app, receipt)
            except Exception as exc:
                _logger.exception(
                    "Child spawn recovery failed", extra={"session_id": receipt.child_id}
                )
                failures.append((receipt.child_id, exc))
        return (receipts[-1].child_id if len(receipts) == limit else None), failures

    async def recover_global_page(
        self,
        app: Any,
        state: SpawnState,
        phase: SpawnPhase,
        *,
        after: tuple[int, str] | None = None,
        limit: int = 100,
    ) -> tuple[tuple[int, str] | None, list[tuple[int, str, Exception]]]:
        refs = await asyncio.to_thread(
            self.spawns.list_global_work, state, phase, after=after, limit=limit
        )
        failures = []
        for ref in refs:
            with workspace_scope(ref.workspace_id), account_authority_scope(None, None):
                try:
                    if state == SpawnState.ADMITTED and await asyncio.to_thread(
                        self.conversations.spawn_was_deleted, ref
                    ):
                        await asyncio.to_thread(self.spawns.retire_deleted_materialization, ref)
                        continue
                    if state == SpawnState.ABORTING:
                        receipt = await asyncio.to_thread(
                            self.spawns.get_claimed_cleanup, ref.child_id
                        )
                    else:
                        receipt = await asyncio.to_thread(
                            self.spawns.get, ref.identity, ref.nonce, epoch=ref.epoch
                        )
                    if receipt is not None:
                        await self.advance(app, receipt)
                except Exception as exc:
                    _logger.exception(
                        "Global child spawn recovery failed", extra={"session_id": ref.child_id}
                    )
                    failures.append((ref.workspace_id, ref.child_id, exc))
        cursor = (refs[-1].workspace_id, refs[-1].child_id) if len(refs) == limit else None
        return cursor, failures


class SessionSpawnRecovery:
    """Recover bounded global pages without requiring a tenant's next HTTP request."""

    _WORK = (
        (SpawnState.ABORTING, SpawnPhase.CLEANUP),
        (SpawnState.ADMITTED, SpawnPhase.MATERIALIZE),
        (SpawnState.ADMITTED, SpawnPhase.ACTIVATE),
        (SpawnState.ADMITTED, SpawnPhase.DISPATCH),
    )

    def __init__(
        self, coordinator: SessionSpawnCoordinator, *, interval_seconds: float = 5
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Spawn recovery interval must be positive")
        self.coordinator = coordinator
        self.interval_seconds = interval_seconds
        self._cursors: dict[tuple[SpawnState, SpawnPhase], tuple[int, str] | None] = {}
        self._expiry_cursor: tuple[int, int, str] | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self, app: Any) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(app), name="session-spawn-recovery")

    def wake(self) -> None:
        self._wake.set()

    async def recover_once(self, app: Any) -> None:
        self._expiry_cursor, _ = await asyncio.to_thread(
            self.coordinator.spawns.expire_pending_page, after=self._expiry_cursor, limit=100
        )
        for state, phase in self._WORK:
            cursor, _ = await self.coordinator.recover_global_page(
                app, state, phase, after=self._cursors.get((state, phase)), limit=100
            )
            self._cursors[state, phase] = cursor
        await asyncio.to_thread(self.coordinator.spawns.cleanup_payloads, all_workspaces=True)

    async def _run(self, app: Any) -> None:
        while True:
            self._wake.clear()
            try:
                await self.recover_once(app)
            except Exception:
                _logger.exception("Child spawn recovery page failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
