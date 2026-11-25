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

from omnigent.db.db_models import workspace_scope
from omnigent.entities.project import Project
from omnigent.server.project_orchestrator import (
    ticket_batch_message,
    ticket_comment_for_worker_message,
    ticket_gate_label_for_merger_message,
    ticket_gate_label_for_worker_message,
)
from omnigent.server.project_router import (
    choose_router,
    project_identities,
    router_session_id,
    router_session_ids,
)
from omnigent.server.session_delivery import deliver_to_session
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.server.ticket_responder import ROUTER, assignee_name, responder_order
from omnigent.server.ticket_review import APPROVED_LABEL, REVIEW_STAGE_ID, REVIEWER_NAME
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore

_logger = logging.getLogger(__name__)
# The persona that lands approved commits; told when a sign-off unblocks one.
MERGER_NAME = "Merger"

DIGEST_SECONDS_ENV = "OMNIGENT_ORCHESTRATOR_DIGEST_SECONDS"
DEFAULT_DIGEST_SECONDS = 180.0
WORKER_REPLY_SECONDS_ENV = "OMNIGENT_TICKET_WORKER_REPLY_SECONDS"
DEFAULT_WORKER_REPLY_SECONDS = 600.0


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        _logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


def _adds_gate_label(event: dict[str, Any]) -> bool:
    """A person's edit that added a sign-off label such as ``risk-ok``."""
    return event["kind"] == "updated" and bool(event["payload"].get("gate_labels_added"))


def _routes_to_workers(event: dict[str, Any]) -> bool:
    """A person's new comment or sign-off label, not yet answered by or escalated past the
    ticket's workers."""
    return (
        event["urgent"]
        and (event["kind"] == "comment" or _adds_gate_label(event))
        and event["session_id"] is None
        and event["agent_id"] is None
        and not event.get("escalated")
    )


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
        worker_reply_seconds: float | None = None,
    ) -> None:
        self.tickets = tickets
        self.projects = projects
        self.inputs = inputs
        self.digest_seconds = (
            _env_seconds(DIGEST_SECONDS_ENV, DEFAULT_DIGEST_SECONDS)
            if digest_seconds is None
            else digest_seconds
        )
        self.worker_reply_seconds = (
            _env_seconds(WORKER_REPLY_SECONDS_ENV, DEFAULT_WORKER_REPLY_SECONDS)
            if worker_reply_seconds is None
            else worker_reply_seconds
        )
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
        now = time.time()
        await asyncio.to_thread(
            self.tickets.escalate_unanswered, before=int(now - self.worker_reply_seconds)
        )
        pending = await asyncio.to_thread(self.tickets.pending_notification_projects)
        due_before = now - self.digest_seconds
        for project in pending:
            digest_due = force_digest or project["oldest_at"] <= due_before
            if not (project["urgent"] or digest_due):
                continue
            with workspace_scope(project["workspace_id"]):
                try:
                    await self._deliver_project(app, project["project_id"], digest_due=digest_due)
                except Exception:
                    # Undelivered events stay pending; a failed session uses its normal UI.
                    _logger.exception(
                        "Could not deliver ticket events for project %s", project["project_id"]
                    )

    async def deliver_to_session(
        self,
        app: Any,
        project: Project,
        session_id: str,
        *,
        stable_id: str,
        text: str,
        automatic: str | None = None,
    ) -> bool:
        """
        Queue one user turn on a session in ``project`` and wake it if idle.

        :returns: ``True`` once something will drain the turn. Sessions that are
            missing, archived or outside the project return ``False``.
        """
        conversation = await asyncio.to_thread(
            self.inputs.conversations.get_conversation, session_id
        )
        if conversation is None or conversation.archived or conversation.project_id != project.id:
            return False
        return await deliver_to_session(
            app,
            self.inputs,
            session_id,
            text,
            user_id=project.user_id,
            stable_id=stable_id,
            automatic=automatic,
        )

    async def _route_to_workers(self, app: Any, project: Project, event: dict[str, Any]) -> bool:
        """Hand a person's comment to the one agent it is most likely meant for.

        ``@Name`` mentions are delivered by the comment route; a comment that names the
        router is left for the router. Otherwise the first candidate from
        :func:`responder_order` that is live gets it, and the rest aren't woken.

        :returns: ``True`` when a non-router session will answer it.
        """
        identities = await project_identities(app, project)
        mentioned = {
            entry.get("id")
            for entry in event["payload"].get("mentions") or []
            if isinstance(entry, dict)
        }
        if mentioned:
            router = choose_router(project, identities)
            return router.identity_id not in mentioned
        session_ids, context = await asyncio.gather(
            asyncio.to_thread(self.tickets.ticket_session_ids, project.id, event["ticket_id"]),
            asyncio.to_thread(
                self.tickets.person_comment_context,
                project.id,
                event["ticket_id"],
                event["position"],
            ),
        )
        by_name = {identity.name.casefold(): identity for identity in identities}
        reviewer = by_name.get(REVIEWER_NAME.casefold())
        wanted = assignee_name(context["labels"])
        assignee = by_name.get(wanted.casefold()) if wanted else None
        order = responder_order(
            linked=session_ids,
            routers=await router_session_ids(app, project),
            reviewer=reviewer.active_session_id if reviewer else None,
            assignee=assignee.active_session_id if assignee else None,
            asker=context["asker"],
            commenters=context["commenters"],
            in_review=context["stage_id"] == REVIEW_STAGE_ID,
        )
        text = (
            ticket_gate_label_for_worker_message(project.id, event)
            if _adds_gate_label(event)
            else ticket_comment_for_worker_message(
                project.id, event, completed=bool(context.get("completed"))
            )
        )
        for session_id in order:
            if session_id is ROUTER:
                return False
            conversation = await asyncio.to_thread(
                self.inputs.conversations.get_conversation, session_id
            )
            if (
                conversation is None
                or conversation.archived
                or conversation.project_id != project.id
            ):
                continue
            # Only the first live candidate gets the turn; if nothing will drain it,
            # the router answers instead of another worker.
            stable_id = hashlib.sha256(f"{event['id']}:{session_id}".encode()).hexdigest()[:32]
            return await self.deliver_to_session(
                app, project, session_id, stable_id=stable_id, text=text
            )
        return False

    async def _tell_merger(self, app: Any, project: Project, event: dict[str, Any]) -> None:
        """Wake the Merger when a person's sign-off unblocks a ticket's approved commit."""
        if not _adds_gate_label(event):
            return
        ticket = await asyncio.to_thread(
            self.tickets.get, project.id, event["ticket_id"], user_id=project.user_id
        )
        ticket = ticket.get("ticket", ticket)
        approved = next(
            (
                label.get("value")
                for label in ticket.get("labels") or []
                if label.get("key") == APPROVED_LABEL and label.get("value")
            ),
            None,
        )
        if approved is None:
            return
        merger = next(
            (
                identity
                for identity in await project_identities(app, project)
                if identity.name.casefold() == MERGER_NAME.casefold()
            ),
            None,
        )
        if merger is None or not merger.active_session_id:
            return
        stable_id = hashlib.sha256(f"{event['id']}:merger".encode()).hexdigest()[:32]
        await self.deliver_to_session(
            app,
            project,
            merger.active_session_id,
            stable_id=stable_id,
            text=ticket_gate_label_for_merger_message(project.id, event, approved),
        )

    async def _deliver_project(self, app: Any, project_id: str, *, digest_due: bool) -> None:
        events = await asyncio.to_thread(self.tickets.project_pending_notifications, project_id)
        if not events:
            return
        project = None
        for user_id in dict.fromkeys(event["user_id"] for event in events):
            project = await asyncio.to_thread(self.projects.get, project_id, user_id=user_id)
            if project is not None:
                break
        if project is None:
            return
        for event in events:
            # Lets the turn name a shared member who acted, as distinct from the owner.
            event["project_owner"] = project.user_id
        remaining = []
        for event in events:
            if _routes_to_workers(event):
                await self._tell_merger(app, project, event)
            if _routes_to_workers(event) and await self._route_to_workers(app, project, event):
                # The orchestrator hears about it in a digest once a worker replies,
                # or urgently if none replies in time.
                await asyncio.to_thread(
                    self.tickets.mark_awaiting_worker, project.id, event["ticket_id"], event["id"]
                )
                continue
            remaining.append(event)
        if not remaining:
            return
        if not digest_due and not any(event["urgent"] for event in remaining):
            return
        router_session = await router_session_id(app, project)
        if router_session is None:
            return
        # A stable input ID derived from the batch means a crash after enqueue but
        # before marking cannot duplicate an agent turn.
        if await self.deliver_to_session(
            app,
            project,
            router_session,
            stable_id=_batch_stable_id(remaining),
            text=ticket_batch_message(
                project.id, remaining, worker_reply_seconds=self.worker_reply_seconds
            ),
            # The board holds everything in this turn, so it may expire.
            automatic="ticket_update",
        ):
            await asyncio.to_thread(
                self.tickets.mark_notified_many,
                project.id,
                [(event["ticket_id"], event["id"]) for event in remaining],
            )
