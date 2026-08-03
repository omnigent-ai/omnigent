"""
Lifecycle manager for freezing, approving, rejecting, and executing deferred actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from omnigent.runtime.deferred import events
from omnigent.runtime.deferred.hashing import compute_manifest_hash
from omnigent.runtime.deferred.models import (
    DeferredAction,
    DeferredActionStatus,
    DeferredAuditEvent,
    DeferredManifest,
)
from omnigent.runtime.deferred.store import DeferredActionStore, get_deferred_store

_logger = logging.getLogger(__name__)

# Default expiration budget for a deferred action (1 hour).
DEFAULT_DEFERRED_EXPIRATION_SECONDS = 3600


class DeferredActionError(Exception):
    """Base exception for deferred action processing errors."""


class DeferredExpiredError(DeferredActionError):
    """Raised when an operation is requested on an expired deferred action."""


class HashDriftError(DeferredActionError):
    """Raised when base state or manifest hash has drifted before approval."""


class DeferredStateError(DeferredActionError):
    """Raised when an invalid state transition is requested."""


class DeferredActionManager:
    """
    Manages deferred action freezing, verification, approval, rejection, and execution.
    """

    def __init__(self, store: DeferredActionStore | None = None) -> None:
        self._store = store or get_deferred_store()
        self._lock = asyncio.Lock()

    async def freeze(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        base_hash: str,
        session_id: str,
        target: str | None = None,
        task_id: str | None = None,
        deciding_policy: str | None = None,
        reason: str | None = None,
        expiration_seconds: int = DEFAULT_DEFERRED_EXPIRATION_SECONDS,
        actor: str = "agent",
    ) -> DeferredAction:
        """
        Freeze a proposed tool call manifest into a deferred action.
        """
        manifest = DeferredManifest(
            tool=tool,
            arguments=arguments,
            base_hash=base_hash,
            session_id=session_id,
            target=target,
        )
        manifest_hash = compute_manifest_hash(manifest)
        action_id = f"def_{secrets.token_hex(16)}"

        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(seconds=expiration_seconds)

        now_str = now_dt.isoformat()
        expires_str = expires_dt.isoformat()

        action = DeferredAction(
            id=action_id,
            manifest=manifest,
            manifest_hash=manifest_hash,
            status="PENDING",
            created_at=now_str,
            expires_at=expires_str,
            session_id=session_id,
            task_id=task_id,
            deciding_policy=deciding_policy,
            reason=reason,
        )

        audit_event = DeferredAuditEvent(
            id=f"evt_{secrets.token_hex(12)}",
            action_id=action_id,
            event_type="CREATE",
            timestamp=now_str,
            actor=actor,
            details={
                "tool": tool,
                "manifest_hash": manifest_hash,
                "reason": reason,
            },
        )

        await self._store.save_action(action)
        await self._store.add_audit_event(audit_event)
        events.emit_deferred_created(session_id, action)

        return action

    async def approve(
        self,
        action_id: str,
        *,
        actor: str,
        current_base_hash: str,
    ) -> DeferredAction:
        """
        Mark a deferred action as APPROVED after verifying expiration and hash matching.

        Requires mandatory current_base_hash to verify against stored state.
        """
        async with self._lock:
            action = await self._store.get_action(action_id)
            if action is None:
                raise DeferredActionError(f"Deferred action {action_id!r} not found")

            # Idempotent return if already approved or executed
            if action.status in ("APPROVED", "EXECUTED"):
                return action

            if action.status != "PENDING":
                raise DeferredStateError(
                    f"Cannot approve action {action_id!r} with status {action.status!r}",
                )

            now_dt = datetime.now(timezone.utc)
            now_str = now_dt.isoformat()

            # 1. Check expiration
            expires_dt = datetime.fromisoformat(action.expires_at)
            if now_dt > expires_dt:
                action.status = "EXPIRED"
                await self._store.save_action(action)
                await self._store.add_audit_event(
                    DeferredAuditEvent(
                        id=f"evt_{secrets.token_hex(12)}",
                        action_id=action_id,
                        event_type="EXPIRE",
                        timestamp=now_str,
                        actor="system",
                        details={"reason": "Approval attempt after expiration"},
                    ),
                )
                events.emit_deferred_expired(action.session_id, action)
                raise DeferredExpiredError(f"Deferred action {action_id!r} has expired")

            # 2. Check hash drift
            current_manifest = DeferredManifest(
                tool=action.manifest.tool,
                arguments=action.manifest.arguments,
                base_hash=current_base_hash,
                session_id=action.manifest.session_id,
                target=action.manifest.target,
            )
            current_hash = compute_manifest_hash(current_manifest)

            if current_hash != action.manifest_hash:
                action.status = "HASH_DRIFT"
                await self._store.save_action(action)
                await self._store.add_audit_event(
                    DeferredAuditEvent(
                        id=f"evt_{secrets.token_hex(12)}",
                        action_id=action_id,
                        event_type="HASH_DRIFT",
                        timestamp=now_str,
                        actor=actor,
                        details={
                            "expected_hash": action.manifest_hash,
                            "current_hash": current_hash,
                            "expected_base_hash": action.manifest.base_hash,
                            "current_base_hash": current_base_hash,
                        },
                    ),
                )
                events.emit_deferred_hash_drift(action.session_id, action)
                raise HashDriftError(
                    f"State drift detected for action {action_id!r}: base state changed",
                )

            # 3. Valid approval
            action.status = "APPROVED"
            await self._store.save_action(action)
            await self._store.add_audit_event(
                DeferredAuditEvent(
                    id=f"evt_{secrets.token_hex(12)}",
                    action_id=action_id,
                    event_type="APPROVE",
                    timestamp=now_str,
                    actor=actor,
                ),
            )
            events.emit_deferred_approved(action.session_id, action)
            return action

    async def execute(
        self,
        action_id: str,
        executor_func: Callable[[], Awaitable[Any]],
    ) -> DeferredAction:
        """
        Execute an APPROVED deferred action callback.

        Transitions to EXECUTED on success, or FAILED if an exception occurs.
        """
        async with self._lock:
            action = await self._store.get_action(action_id)
            if action is None:
                raise DeferredActionError(f"Deferred action {action_id!r} not found")

            if action.status == "EXECUTED":
                return action

            if action.status != "APPROVED":
                raise DeferredStateError(
                    f"Cannot execute action {action_id!r} in status {action.status!r}",
                )

            now_str = datetime.now(timezone.utc).isoformat()

            try:
                await executor_func()
                action.status = "EXECUTED"
                await self._store.save_action(action)
                await self._store.add_audit_event(
                    DeferredAuditEvent(
                        id=f"evt_{secrets.token_hex(12)}",
                        action_id=action_id,
                        event_type="EXECUTE",
                        timestamp=now_str,
                        actor="system",
                    ),
                )
                events.emit_deferred_executed(action.session_id, action)
                return action
            except Exception as exc:
                action.status = "FAILED"
                action.error_message = str(exc)
                await self._store.save_action(action)
                await self._store.add_audit_event(
                    DeferredAuditEvent(
                        id=f"evt_{secrets.token_hex(12)}",
                        action_id=action_id,
                        event_type="FAIL",
                        timestamp=now_str,
                        actor="system",
                        details={"error": str(exc)},
                    ),
                )
                events.emit_deferred_failed(action.session_id, action)
                raise

    async def reject(
        self,
        action_id: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> DeferredAction:
        """
        Reject a pending deferred action.
        """
        async with self._lock:
            action = await self._store.get_action(action_id)
            if action is None:
                raise DeferredActionError(f"Deferred action {action_id!r} not found")

            if action.status == "REJECTED":
                return action

            if action.status not in ("PENDING", "APPROVED"):
                raise DeferredStateError(
                    f"Cannot reject action {action_id!r} with status {action.status!r}",
                )

            now_str = datetime.now(timezone.utc).isoformat()
            action.status = "REJECTED"
            action.reason = reason or action.reason
            await self._store.save_action(action)
            await self._store.add_audit_event(
                DeferredAuditEvent(
                    id=f"evt_{secrets.token_hex(12)}",
                    action_id=action_id,
                    event_type="REJECT",
                    timestamp=now_str,
                    actor=actor,
                    details={"reason": reason},
                ),
            )
            events.emit_deferred_rejected(action.session_id, action)
            return action
