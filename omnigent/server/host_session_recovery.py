"""Respond to a ready host's request to restore its interrupted sessions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation
from omnigent.server.schemas import ErrorDetail
from omnigent.session_recovery import (
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_ATTEMPT_S,
    RECOVERY_MAX_AGE_S,
    RECOVERY_RETRY_S,
    SessionRecovery,
)
from omnigent.stores.conversation_store import runner_seen_is_fresh
from omnigent.util.session_lifecycle import is_session_closed

if TYPE_CHECKING:
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.host_registry import HostConnection, HostRegistry
    from omnigent.server.runner_session_init import RunnerSessionInitializer
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)


def record_interruption(store: ConversationStore, conv: Conversation) -> None:
    """Retain interruption intent even when orphan settlement later clears status."""
    from omnigent.server.child_session_recovery import _restorable

    if (
        not conv.host_id
        or not conv.runner_id
        or conv.parent_conversation_id is not None
        or not _restorable(conv)
        or conv.pending_elicitation_count
        or not (
            conv.live_status in {"running", "waiting"}
            or conv.labels.get("omnigent.last_task_error_code") == "runner_disconnected"
        )
    ):
        return
    previous = store.get_session_recovery(conv.id)
    if previous is None:
        recovery = SessionRecovery(conv.host_id, conv.runner_id, now_epoch())
    elif previous.phase == "submitted" and previous.runner_id == conv.runner_id:
        recovery = replace(previous, phase="pending", attempt_id=None)
    else:
        return
    store.replace_session_recovery(conv.id, previous, recovery)


def stop_recovery(
    store: ConversationStore, conv: Conversation, *, only_pending: bool = False
) -> None:
    """Persist Stop before the process signal, including when its host is offline."""
    if conv.host_id is None or conv.runner_id is None:
        return
    while True:
        previous = store.get_session_recovery(conv.id)
        if previous is None and only_pending:
            return
        stopped = replace(
            previous or SessionRecovery(conv.host_id, conv.runner_id, now_epoch()), phase="stopped"
        )
        if store.replace_session_recovery(conv.id, previous, stopped):
            return
        # Stores without this optional contract must keep their normal Stop path.
        if store.get_session_recovery(conv.id) == previous:
            return


class HostSessionRecoverer:
    """One bounded pass per host request; never provisions or wakes compute."""

    def __init__(
        self,
        store: ConversationStore,
        hosts: HostRegistry,
        host_store: HostStore,
        runners: RunnerRouter,
        initializer: RunnerSessionInitializer,
    ) -> None:
        self.store = store
        self.hosts = hosts
        self.host_store = host_store
        self.runners = runners
        self.initializer = initializer

    async def recover(self, connection: HostConnection, live_runners: list[str]) -> None:
        host = await asyncio.to_thread(self.host_store.get_host, connection.host_id)
        if (
            not self.store.supports_session_recovery
            or not connection.hello.recover_interrupted_sessions
            or host is None
            or host.sandbox_provider is None
            or host.deleted_at is not None
            or host.user_id != connection.owner
        ):
            return
        after = None
        while self.hosts.get(connection.host_id) is connection:
            ids = await asyncio.to_thread(
                self.store.list_host_conversation_ids, connection.host_id, after=after
            )
            for session_id in ids:
                if self.hosts.get(connection.host_id) is not connection:
                    return
                try:
                    await self._recover_session(connection, session_id, live_runners)
                except Exception:
                    _logger.exception("Host session recovery failed for %s", session_id)
            if len(ids) < 32:
                return
            after = ids[-1]

    async def _recover_session(
        self, connection: HostConnection, session_id: str, live_runners: list[str]
    ) -> None:
        from omnigent.server.child_session_recovery import _restorable
        from omnigent.server.routes.sessions import (
            _launch_runner_on_host,
            _query_host_runner_status,
        )

        conv = await asyncio.to_thread(self.store.get_conversation, session_id)
        if (
            conv is None
            or conv.host_id != connection.host_id
            or conv.parent_conversation_id is not None
            or not _restorable(conv)
            or conv.pending_elicitation_count
            or is_session_closed(conv.labels, conv.title)
        ):
            return
        owner = await asyncio.to_thread(self.store.get_session_owner, session_id, owner_only=True)
        if owner is not None and owner != connection.owner:
            return
        if await asyncio.to_thread(self.store.list_pending_inputs, session_id):
            return
        recovery = await asyncio.to_thread(self.store.get_session_recovery, session_id)
        online = bool(conv.runner_id and self.runners.runner_is_online(conv.runner_id))
        if recovery is not None and recovery.phase == "launching" and online:
            if recovery.runner_id == conv.runner_id:
                await self._continue(connection, conv, recovery)
            return
        if online or conv.runner_id in live_runners or runner_seen_is_fresh(conv.runner_last_seen):
            return
        if recovery is None:
            await asyncio.to_thread(record_interruption, self.store, conv)
            recovery = await asyncio.to_thread(self.store.get_session_recovery, session_id)
        if (
            recovery is None
            or recovery.host_id != connection.host_id
            or recovery.runner_id != conv.runner_id
        ):
            return
        if recovery.phase in {"failed", "stopped"}:
            return
        if recovery.phase == "dispatching":
            if recovery.retry_at <= now_epoch():
                await self._fail(conv, recovery, "Continuation delivery could not be confirmed.")
            return
        if conv.runner_id:
            status = await _query_host_runner_status(connection, self.hosts, conv.runner_id)
            if status not in {"dead", "unknown"}:
                return
        if recovery.phase == "submitted":
            await asyncio.to_thread(record_interruption, self.store, conv)
            recovery = await asyncio.to_thread(self.store.get_session_recovery, session_id)
            if recovery is None or recovery.phase != "pending":
                return
        now = now_epoch()
        if recovery.retry_at > now:
            return
        if (
            recovery.attempts >= MAX_RECOVERY_ATTEMPTS
            or now - recovery.interrupted_at > RECOVERY_MAX_AGE_S
        ):
            await self._fail(
                conv, recovery, "Automatic session recovery exhausted its retry budget."
            )
            return
        attempt = replace(
            recovery,
            phase="launching",
            attempts=recovery.attempts + 1,
            attempt_id=uuid4().hex,
            retry_at=now + RECOVERY_ATTEMPT_S,
        )
        if not await asyncio.to_thread(
            self.store.replace_session_recovery, session_id, recovery, attempt
        ):
            return
        try:
            async with asyncio.timeout(RECOVERY_ATTEMPT_S):
                # Reuse admission and binding tokens; do not call ensure_runner_connected,
                # whose lazy wake path is allowed to mutate provider compute.
                launch = await _launch_runner_on_host(
                    conv, self.store, self.hosts, connection, recovery=attempt
                )
                current = await asyncio.to_thread(self.store.get_session_recovery, session_id)
                if current is None or current.attempt_id != attempt.attempt_id:
                    return
                if current.phase != "launching":
                    return
                attempt = current
                if launch.error or launch.error_code:
                    raise RuntimeError(launch.error or launch.error_code)
                while not self.runners.runner_is_online(launch.runner_id):
                    if self.hosts.get(connection.host_id) is not connection:
                        return
                    if (
                        await asyncio.to_thread(self.store.get_session_recovery, session_id)
                        != attempt
                    ):
                        return
                    await asyncio.sleep(0.25)
                fresh = await asyncio.to_thread(self.store.get_conversation, session_id)
                if (
                    fresh is None
                    or fresh.host_id != connection.host_id
                    or fresh.runner_id != launch.runner_id
                    or not _restorable(fresh)
                    or fresh.pending_elicitation_count
                ):
                    return
                await self._continue(connection, fresh, attempt)
        except (TimeoutError, RuntimeError, ConnectionError) as exc:
            retry = replace(
                attempt,
                phase="pending",
                retry_at=now_epoch() + RECOVERY_RETRY_S * 2 ** (attempt.attempts - 1),
            )
            await asyncio.to_thread(
                self.store.replace_session_recovery, session_id, attempt, retry
            )
            if attempt.attempts >= MAX_RECOVERY_ATTEMPTS:
                await self._fail(conv, retry, f"Automatic session recovery stopped: {exc}")

    async def _continue(
        self, connection: HostConnection, conv: Conversation, attempt: SessionRecovery
    ) -> None:
        import httpx

        assert conv.runner_id is not None
        if self.hosts.get(connection.host_id) is not connection:
            return
        routed = await asyncio.to_thread(self.runners.client_for_existing_conversation, conv.id)
        if routed is None or routed.runner_id != conv.runner_id:
            raise ConnectionError("runner disconnected before continuation")
        dispatch = replace(attempt, phase="dispatching", runner_id=conv.runner_id)
        if not await asyncio.to_thread(
            self.store.replace_session_recovery, conv.id, attempt, dispatch
        ):
            return
        try:
            response = await self.initializer.initialize(
                conv,
                routed.client,
                timeout=60,
                resume_interrupted_turn=True,
                recovery_id=attempt.attempt_id,
                trigger="host_recovery",
            )
            response.raise_for_status()
        except (httpx.HTTPError, ConnectionError):
            await self._fail(conv, dispatch, "Continuation delivery could not be confirmed.")
            return
        submitted = response.json().get("recovery_turn") == "recovery_prompt"
        await asyncio.to_thread(
            self.store.replace_session_recovery,
            conv.id,
            dispatch,
            replace(dispatch, phase="submitted" if submitted else "stopped"),
        )

    async def _fail(self, conv: Conversation, recovery: SessionRecovery, reason: str) -> None:
        from omnigent.server.routes.sessions import (
            _persist_session_status_error_labels,
            _publish_status,
        )

        if not await asyncio.to_thread(
            self.store.replace_session_recovery,
            conv.id,
            recovery,
            replace(recovery, phase="failed"),
        ):
            return
        error = ErrorDetail(
            code="session_recovery_failed", message=f"{reason} Send a message to retry."
        )
        _publish_status(conv.id, "failed", error)
        await _persist_session_status_error_labels(conv.id, error, self.store)
