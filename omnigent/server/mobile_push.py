"""Server-owned attention seam and off-path, leased FCM delivery worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from time import monotonic
from typing import Any

from omnigent.db.db_models import workspace_scope
from omnigent.entities.conversation import MessageData
from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state
from omnigent.server.mobile_push_content import failure_reason, message_payload
from omnigent.server.mobile_push_sender import FcmSender, SendResult
from omnigent.server.mobile_push_store import Delivery, MobilePushStore
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)
_service: MobilePushService | None = None


def observe_status(
    session_id: str, previous: str | None, status: str, error_code: str | None
) -> None:
    service = _service
    if service is not None:
        service.status(session_id, previous, status, error_code)


def observe_input(session_id: str) -> None:
    service = _service
    if service is not None:
        session_live_state.submit("mobile_push_cancel_input", service.store.cancel, session_id)


class MobilePushService:
    def __init__(
        self,
        store: MobilePushStore,
        conversation_store: ConversationStore,
        sender: FcmSender,
        *,
        preview: bool,
    ) -> None:
        self.store = store
        self.conversation_store = conversation_store
        self.sender = sender
        self.preview = preview
        self._task: asyncio.Task[None] | None = None
        self._remove_observer: Callable[[], None] | None = None
        self._purge_at = 0.0

    def status(
        self, session_id: str, previous: str | None, status: str, error_code: str | None
    ) -> None:
        if status in {"running", "waiting"}:
            session_live_state.submit(
                "mobile_push_cancel_activity", self._cancel_activity, session_id
            )
        elif previous in {"running", "waiting"} and status in {"idle", "failed"}:
            kind = "completed" if status == "idle" else "failed"
            session_live_state.submit(
                "mobile_push_enqueue_terminal",
                self.store.enqueue,
                session_id,
                kind,
                failure_reason(error_code),
            )

    def elicitation(self, session_id: str, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "response.elicitation_request":
            session_live_state.submit(
                "mobile_push_enqueue_prompt", self.store.enqueue, session_id, "needs_input"
            )
        elif event_type == "response.elicitation_resolved":
            session_live_state.submit(
                "mobile_push_resolve_prompt", self._cancel_resolved, session_id
            )

    def _cancel_resolved(self, session_id: str) -> None:
        self.store.cancel(session_id, prompts_only=True)

    def _cancel_activity(self, session_id: str) -> None:
        self.store.cancel(session_id, terminal_only=True)

    async def start(self) -> None:
        global _service
        _service = self
        self._remove_observer = pending_elicitations.add_elicitation_observer(self.elicitation)
        self._task = asyncio.create_task(self._run(), name="mobile-push-delivery")
        self._task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            error = task.exception()
            _logger.error(
                "Mobile push delivery task ended unexpectedly: %s",
                type(error).__name__,
            )

    async def stop(self) -> None:
        global _service
        if _service is self:
            _service = None
        if self._remove_observer is not None:
            self._remove_observer()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.sender.client.aclose()

    async def _run(self) -> None:
        while True:
            productive = 0
            try:
                productive = await self.deliver_once()
            except Exception as error:
                _logger.exception(
                    "Mobile push delivery iteration failed; will retry",
                    exc_info=(Exception, Exception(type(error).__name__), None),
                )
            if not productive:
                await asyncio.sleep(1)

    def _preview(self, session_id: str) -> str | None:
        cursor = None
        while True:
            page = self.conversation_store.list_items(
                session_id, limit=100, after=cursor, order="desc", type="message"
            )
            for item in page.data:
                if isinstance(item.data, MessageData) and item.data.role == "assistant":
                    text = "\n".join(
                        block["text"]
                        for block in item.data.content
                        if block.get("type") in {"text", "output_text"}
                        and isinstance(block.get("text"), str)
                    )
                    return text[:120] or None
            if not page.has_more or not page.last_id:
                return None
            cursor = page.last_id

    async def deliver_once(self, *, now: int | None = None) -> int:
        if monotonic() >= self._purge_at:
            for _ in range(20):
                removed = await asyncio.to_thread(self.store.purge_expired, now=now, limit=50)
                if removed < 50:
                    break
            self._purge_at = monotonic() + 60
        claimed = 0
        for workspace in await asyncio.to_thread(self.store.pending_workspaces, now=now):
            with workspace_scope(workspace):
                deliveries = await asyncio.to_thread(self.store.claim, now=now, limit=8)
                claimed += len(deliveries)
                async with asyncio.TaskGroup() as group:
                    for delivery in deliveries:
                        group.create_task(self._deliver_safely(delivery, now=now))
        return claimed

    async def _deliver_safely(self, delivery: Delivery, *, now: int | None) -> None:
        try:
            await self._deliver(delivery, now=now)
        except Exception as error:
            _logger.exception(
                "Mobile push delivery failed; will retry",
                exc_info=(Exception, Exception(type(error).__name__), None),
            )
            try:
                await asyncio.to_thread(self.store.acknowledge, delivery, "retry", now=now)
            except Exception as error:
                _logger.exception(
                    "Mobile push retry acknowledgement failed",
                    exc_info=(Exception, Exception(type(error).__name__), None),
                )

    async def _deliver(self, delivery: Delivery, *, now: int | None) -> None:
        result = SendResult("discard")
        for _ in range(2):
            token = await self.sender.authorization()
            if isinstance(token, SendResult):
                result = token
                break
            preview = (
                await asyncio.to_thread(self._preview, delivery.session_id)
                if self.preview
                else None
            )
            prepared = await asyncio.to_thread(self.store.prepare, delivery, now=now)
            if prepared is None:
                result = SendResult("discard")
                break
            payload = message_payload(
                platform=prepared.device.platform,
                token=prepared.device.token,
                session_id=delivery.session_id,
                kind=delivery.kind,
                title=prepared.title,
                reason=delivery.reason,
                preview=preview,
            )
            # Authority can change in the irreducible final-check-to-POST window.
            # At-least-once: crash after send can duplicate; before intent commit can lose.
            result = await self.sender.post(payload, token)
            if result.outcome != "refresh":
                break
        if result.outcome == "refresh":
            self.sender.warn_auth_failure(401)
            result = SendResult("discard")
        assert result.outcome != "refresh"
        await asyncio.to_thread(
            self.store.acknowledge,
            delivery,
            result.outcome,
            retry_after=result.retry_after,
            now=now,
        )
