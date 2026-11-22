"""Drain ticket events into the existing durable session-input queue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from starlette.datastructures import Headers

from omnigent.db.db_models import workspace_scope
from omnigent.server.project_orchestrator import ticket_update_message
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore

_logger = logging.getLogger(__name__)


class ProjectTicketNotifications:
    def __init__(
        self, tickets: ProjectTicketStore, projects: ProjectStore, inputs: ManagedStartupInputs
    ) -> None:
        self.tickets = tickets
        self.projects = projects
        self.inputs = inputs
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._after: tuple[int, str, str, int, str] | None = None

    def start(self, app: Any) -> None:
        async def run() -> None:
            while True:
                try:
                    await self.flush_page(app)
                except Exception:
                    _logger.exception("Could not queue project ticket updates")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                self._wake.clear()

        self._task = asyncio.create_task(run(), name="project-ticket-notifications")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def flush_page(self, app: Any) -> None:
        events = await asyncio.to_thread(self.tickets.pending_notifications, after=self._after)
        for event in events:
            self._after = (
                event["workspace_id"],
                event["project_id"],
                event["ticket_id"],
                event["position"],
                event["id"],
            )
            with workspace_scope(event["workspace_id"]):
                try:
                    project = await asyncio.to_thread(
                        self.projects.get, event["project_id"], user_id=event["user_id"]
                    )
                    if project is None or project.main_session_id is None:
                        continue
                    session_id = project.main_session_id
                    conversation = await asyncio.to_thread(
                        self.inputs.conversations.get_conversation, session_id
                    )
                    if (
                        conversation is None
                        or conversation.archived
                        or conversation.project_id != project.id
                    ):
                        continue
                    # The event UUID is the stable input ID: a crash after enqueue
                    # but before mark_notified cannot duplicate an agent turn.
                    await self.inputs.enqueue(
                        session_id,
                        SessionEventInput(
                            type="message",
                            data={
                                "role": "user",
                                "stable_id": event["id"],
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": ticket_update_message(project.id, event),
                                    }
                                ],
                            },
                        ),
                        user_id=project.user_id,
                        headers=Headers(),
                        first=False,
                    )
                    scheduled = await self.inputs.deliver_pending(app, session_id)
                    # Host workers recover queued inputs after reconnecting. A direct
                    # runner's owning replica must start its drain before retiring the event.
                    if conversation.host_id is not None or scheduled:
                        await asyncio.to_thread(
                            self.tickets.mark_notified, project.id, event["ticket_id"], event["id"]
                        )
                except Exception:
                    # The ticket remains committed and its undelivered event remains
                    # pending. A disconnected/failed session uses its normal UI.
                    _logger.exception("Could not deliver ticket event %s", event["id"])
        if len(events) < 32:
            self._after = None
        else:
            self.wake()
