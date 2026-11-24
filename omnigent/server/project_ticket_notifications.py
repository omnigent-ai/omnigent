"""Drain ticket events into the existing durable session-input queue.

Urgent events wake the project's main conversation at once and carry any batched
events with them; the rest wait for one periodic digest turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from typing import Any

from starlette.datastructures import Headers

from omnigent.db.db_models import workspace_scope
from omnigent.server.project_orchestrator import ticket_batch_message
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore

_logger = logging.getLogger(__name__)

DIGEST_SECONDS_ENV = "OMNIGENT_ORCHESTRATOR_DIGEST_SECONDS"
DEFAULT_DIGEST_SECONDS = 180.0


def _digest_seconds() -> float:
    raw = os.environ.get(DIGEST_SECONDS_ENV, "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_DIGEST_SECONDS
    except ValueError:
        _logger.warning("Ignoring invalid %s=%r", DIGEST_SECONDS_ENV, raw)
        return DEFAULT_DIGEST_SECONDS


def _batch_stable_id(events: list[dict[str, Any]]) -> str:
    """One event keeps its own ID; a batch hashes its members so a retry dedupes exactly."""
    if len(events) == 1:
        return str(events[0]["id"])
    joined = ",".join(sorted(str(event["id"]) for event in events))
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


class ProjectTicketNotifications:
    def __init__(
        self,
        tickets: ProjectTicketStore,
        projects: ProjectStore,
        inputs: ManagedStartupInputs,
        *,
        digest_seconds: float | None = None,
    ) -> None:
        self.tickets = tickets
        self.projects = projects
        self.inputs = inputs
        self.digest_seconds = _digest_seconds() if digest_seconds is None else digest_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

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

    async def flush_page(self, app: Any, *, force_digest: bool = False) -> None:
        """Deliver projects with an urgent event, or whose oldest batched event is due."""
        pending = await asyncio.to_thread(self.tickets.pending_notification_projects)
        due_before = time.time() - self.digest_seconds
        for project in pending:
            if not (project["urgent"] or force_digest or project["oldest_at"] <= due_before):
                continue
            with workspace_scope(project["workspace_id"]):
                try:
                    await self._deliver_project(app, project["project_id"])
                except Exception:
                    # Undelivered events stay pending; a failed session uses its normal UI.
                    _logger.exception(
                        "Could not deliver ticket events for project %s", project["project_id"]
                    )

    async def _deliver_project(self, app: Any, project_id: str) -> None:
        events = await asyncio.to_thread(self.tickets.project_pending_notifications, project_id)
        if not events:
            return
        project = None
        for user_id in dict.fromkeys(event["user_id"] for event in events):
            project = await asyncio.to_thread(self.projects.get, project_id, user_id=user_id)
            if project is not None:
                break
        if project is None or project.main_session_id is None:
            return
        session_id = project.main_session_id
        conversation = await asyncio.to_thread(
            self.inputs.conversations.get_conversation, session_id
        )
        if conversation is None or conversation.archived or conversation.project_id != project.id:
            return
        # A stable input ID derived from the batch means a crash after enqueue but
        # before marking cannot duplicate an agent turn.
        await self.inputs.enqueue(
            session_id,
            SessionEventInput(
                type="message",
                data={
                    "role": "user",
                    "stable_id": _batch_stable_id(events),
                    "content": [
                        {"type": "input_text", "text": ticket_batch_message(project.id, events)}
                    ],
                },
            ),
            user_id=project.user_id,
            headers=Headers(),
            first=False,
        )
        scheduled = await self.inputs.deliver_pending(app, session_id)
        # Host workers recover queued inputs after reconnecting. A direct runner's
        # owning replica must start its drain before retiring the events.
        if conversation.host_id is not None or scheduled:
            await asyncio.to_thread(
                self.tickets.mark_notified_many,
                project.id,
                [(event["ticket_id"], event["id"]) for event in events],
            )
