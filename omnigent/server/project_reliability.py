"""Check in on stalled project work and escalate to the project's lead orchestrator.

A server-run, non-LLM loop. Every check it looks at each project with an
orchestrator and:

- finds tickets in a working stage (any stage between the first and the
  completed one) whose ticket and linked workers have shown no activity for the
  stage's threshold, while no linked worker is running;
- walks an escalation ladder for each such ticket: nudge the worker, then
  restart its runner (the Reload tools path), then escalate to the lead with a
  one-line summary. Each step waits ``step_minutes`` and is recorded on the
  ticket as an agent note. Any new activity resets the ladder;
- watches the lead itself: when it failed, its main session is gone, or a
  queued message has waited ``lead_response_minutes`` while it sat idle, the
  loop restarts it, then rolls it over, or restores a deleted one;
- gives the lead a short periodic review turn every ``review_hours`` when the
  board changed since the last one.

Settings live in project config ``reliability`` (see :func:`settings`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from omnigent.entities.project_ticket import CommentOnTicket, TicketActor

_logger = logging.getLogger(__name__)

CONFIG_KEY = "reliability"
NOTE_PREFIX = "Reliability check"
_CHECK_SECONDS = float(os.environ.get("OMNIGENT_RELIABILITY_CHECK_SECONDS", "60"))
_ENABLED = os.environ.get("OMNIGENT_RELIABILITY_LOOP", "1") not in ("0", "false", "off")
_MID_TURN = ("running", "waiting")
_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "stall_minutes": 30.0,
    "stage_minutes": {},
    "step_minutes": 15.0,
    "lead_response_minutes": 15.0,
    "review_hours": 4.0,
}
NUDGE, RESTART, ESCALATE = 1, 2, 3
LEAD_RESTART, LEAD_ROLLOVER = 1, 2


@dataclass
class Settings:
    enabled: bool
    stall_s: float
    stage_s: dict[str, float]
    step_s: float
    lead_response_s: float
    review_s: float

    def threshold(self, stage_id: str) -> float:
        return self.stage_s.get(stage_id, self.stall_s)


def settings(config: dict[str, Any]) -> Settings:
    """Read ``config["reliability"]``; any missing or invalid value uses its default."""
    raw = config.get(CONFIG_KEY)
    raw = raw if isinstance(raw, dict) else {}

    def number(key: str) -> float:
        value = raw.get(key, _DEFAULTS[key])
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        return float(value) if ok else float(_DEFAULTS[key])

    stages = raw.get("stage_minutes")
    stage_s = {
        str(stage): float(minutes) * 60
        for stage, minutes in (stages.items() if isinstance(stages, dict) else [])
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0
    }
    return Settings(
        enabled=raw.get("enabled", True) is not False,
        stall_s=number("stall_minutes") * 60,
        stage_s=stage_s,
        step_s=number("step_minutes") * 60,
        lead_response_s=number("lead_response_minutes") * 60,
        review_s=number("review_hours") * 3600,
    )


@dataclass
class TicketLadder:
    step: int = 0
    at: float = 0.0
    ticket_mark: int = 0
    worker_marks: dict[str, int] = field(default_factory=dict)


@dataclass
class LeadLadder:
    step: int = 0
    at: float = 0.0
    session_id: str | None = None


@dataclass
class ProjectState:
    tickets: dict[str, TicketLadder] = field(default_factory=dict)
    lead: LeadLadder = field(default_factory=LeadLadder)
    started_at: float = field(default_factory=time.time)
    last_review_at: float = 0.0
    reviewed_mark: int = 0
    escalated: set[str] = field(default_factory=set)


@dataclass
class SessionView:
    id: str
    status: str
    activity_at: int
    position: int
    dead: bool
    oldest_pending_at: int | None


class ReliabilityLoop:
    """One per server process; :meth:`check_all` is safe to call directly in tests."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.state: dict[str, ProjectState] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    # -- plumbing ----------------------------------------------------------

    @property
    def _notifications(self) -> Any:
        return getattr(self.app.state, "project_ticket_notifications", None)

    def start(self) -> None:
        async def run() -> None:
            while True:
                await asyncio.sleep(_CHECK_SECONDS)
                try:
                    await self.check_all()
                except Exception:
                    _logger.exception("Reliability check failed")

        self._task = asyncio.create_task(run(), name="project-reliability-loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def check_all(self, now: float | None = None) -> None:
        notifications = self._notifications
        if notifications is None or not _ENABLED:
            return
        async with self._lock:
            for project_id, user_id, _main in await asyncio.to_thread(
                notifications.projects.list_main_sessions
            ):
                try:
                    await self.check_project(project_id, user_id, now=now)
                except Exception:
                    _logger.exception("Reliability check failed for project %s", project_id)

    async def _session(self, session_id: str) -> SessionView | None:
        from omnigent.server.routes._sessions.common import _session_status_cache
        from omnigent.server.routes._sessions.orchestration import _session_is_starting
        from omnigent.server.startup_inputs import STARTUP_LABEL

        conversations = self._notifications.inputs.conversations
        conv = await asyncio.to_thread(conversations.get_conversation, session_id)
        if conv is None or conv.archived:
            return None
        status = _session_status_cache.get(conv.id, conv.live_status) or "idle"
        if _session_is_starting(conv.id):
            status = "running"
        pending = await asyncio.to_thread(conversations.list_pending_inputs, conv.id)
        waiting = [item.created_at for item in pending if item.status == "in_progress"]
        return SessionView(
            id=conv.id,
            status=status,
            activity_at=int(conv.updated_at or 0),
            position=int(conv.next_position or 0),
            dead=status == "failed" or conv.labels.get(STARTUP_LABEL) == "failed",
            oldest_pending_at=min(waiting) if waiting else None,
        )

    async def _send(self, session_id: str, text: str, user_id: str | None) -> None:
        from starlette.datastructures import Headers

        from omnigent.server.schemas import SessionEventInput

        inputs = self._notifications.inputs
        await inputs.enqueue(
            session_id,
            SessionEventInput(
                type="message",
                data={"role": "user", "content": [{"type": "input_text", "text": text}]},
            ),
            user_id=user_id,
            headers=Headers(),
            first=False,
        )
        await inputs.deliver_pending(self.app, session_id)

    async def _note(self, project: Any, ticket_id: str, text: str) -> int:
        """Record *text* on the ticket as an agent note; return its new ``updated_at``."""
        result = await asyncio.to_thread(
            self._notifications.tickets.comment,
            project.id,
            ticket_id,
            CommentOnTicket(text=f"{NOTE_PREFIX}: {text}"[:2000], audience="agents"),
            TicketActor(user_id=project.user_id),
        )
        return int(result.get("updated_at") or time.time())

    async def _restart(self, session_id: str, user_id: str | None) -> bool:
        """Replace the session's runner once it is idle (the Reload tools path)."""
        from omnigent.server.project_orchestrator_rollover import _owner_request
        from omnigent.server.routes._sessions import tool_reload

        conversations = self._notifications.inputs.conversations
        conv = await asyncio.to_thread(conversations.get_conversation, session_id)
        if conv is None:
            return False
        if tool_reload.tool_reload_pending(session_id):
            return True
        try:
            await tool_reload.require_reloadable(conv, conversations)
        except Exception:  # noqa: BLE001 — not restartable here; the ladder moves on
            return False
        tool_reload.schedule_tool_reload(
            _owner_request(self.app, uuid.uuid4().hex),
            session_id,
            user_id=user_id,
            conversation_store=conversations,
            runner_router=getattr(self.app.state, "runner_router", None),
            permission_store=getattr(self.app.state, "permission_store", None),
        )
        return True

    # -- per project -------------------------------------------------------

    async def check_project(
        self, project_id: str, user_id: str | None, *, now: float | None
    ) -> None:
        now = time.time() if now is None else now
        notifications = self._notifications
        project = await asyncio.to_thread(notifications.projects.get, project_id, user_id=user_id)
        if project is None:
            return
        config = settings(project.config)
        state = self.state.setdefault(project.id, ProjectState(started_at=now))
        if not config.enabled:
            state.tickets.clear()
            return
        lead_ok = await self._check_lead(project, state, config, now)
        board = await asyncio.to_thread(
            notifications.tickets.list, project.id, user_id=project.user_id, limit=100
        )
        stages = board.get("stages", [])
        if len(stages) < 2:
            return
        working = {stage["id"]: stage["name"] for stage in stages[1:-1]}
        excluded = {project.main_session_id, *project.retired_main_session_ids}
        seen: set[str] = set()
        newest = 0
        for ticket in board.get("data", []):
            newest = max(newest, int(ticket.get("updated_at") or 0))
            if ticket.get("stage_id") not in working:
                continue
            seen.add(ticket["id"])
            await self._check_ticket(
                project, state, config, ticket, working[ticket["stage_id"]], excluded, now
            )
        for ticket_id in set(state.tickets) - seen:
            state.tickets.pop(ticket_id, None)
            state.escalated.discard(ticket_id)
        await self._maybe_review(project, state, config, newest, lead_ok, now)

    async def _workers(
        self, project: Any, ticket_id: str, excluded: set[Any]
    ) -> list[SessionView]:
        linked = await asyncio.to_thread(
            self._notifications.tickets.sessions,
            project.id,
            ticket_id,
            user_id=project.user_id,
            limit=20,
        )
        views = []
        for entry in linked.get("data", []):
            if entry["session_id"] in excluded:
                continue
            view = await self._session(entry["session_id"])
            if view is not None:
                views.append(view)
        return views

    async def _check_ticket(
        self,
        project: Any,
        state: ProjectState,
        config: Settings,
        ticket: dict[str, Any],
        stage_name: str,
        excluded: set[Any],
        now: float,
    ) -> None:
        ticket_id = ticket["id"]
        ladder = state.tickets.get(ticket_id)
        updated = int(ticket.get("updated_at") or 0)
        workers = await self._workers(project, ticket_id, excluded)
        running = [w for w in workers if w.status in _MID_TURN]
        progressed = ladder is not None and (
            updated > ladder.ticket_mark
            or any(w.position > ladder.worker_marks.get(w.id, w.position) for w in workers)
        )
        if running or progressed:
            if ladder is not None:
                state.tickets.pop(ticket_id, None)
                state.escalated.discard(ticket_id)
            return
        if ladder is None:
            last = max([updated, *(w.activity_at for w in workers)])
            idle_s = now - last
            if idle_s < config.threshold(ticket["stage_id"]):
                return
            ladder = TicketLadder()
            state.tickets[ticket_id] = ladder
        elif now - ladder.at < config.step_s:
            return
        minutes = int((now - max([updated, *(w.activity_at for w in workers)])) // 60)
        title = (ticket.get("title") or "")[:80]
        alive = [w for w in workers if not w.dead]
        dead = [w for w in workers if w.dead]
        next_step = ladder.step + 1
        if next_step == NUDGE and not alive:
            next_step = RESTART if dead else ESCALATE
        if next_step == RESTART and not workers:
            next_step = ESCALATE
        if next_step > ESCALATE:
            return
        if next_step == NUDGE:
            for worker in alive:
                await self._send(
                    worker.id,
                    f'[{NOTE_PREFIX}] Ticket {ticket_id} ("{title}") has had no activity for '
                    f"{minutes} min while you were idle. If you're still on it, continue; if "
                    "you're blocked or done, update the ticket and send your coordinator a "
                    "handoff now.",
                    project.user_id,
                )
            text = f"no activity for {minutes} min in {stage_name}; nudged " + ", ".join(
                f"`{w.id[:8]}`" for w in alive
            )
        elif next_step == RESTART:
            restarted = [w for w in workers if await self._restart(w.id, project.user_id)]
            for worker in restarted:
                await self._send(
                    worker.id,
                    f"[{NOTE_PREFIX}] Your session was restarted because ticket {ticket_id} "
                    f"stalled for {minutes} min. Continue the work, or update the ticket and "
                    "hand off to your coordinator.",
                    project.user_id,
                )
            if not restarted:
                next_step = ESCALATE
            text = f"still no activity after {minutes} min; restarted " + (
                ", ".join(f"`{w.id[:8]}`" for w in restarted) or "nothing"
            )
        if next_step == ESCALATE:
            if ticket_id in state.escalated or project.main_session_id is None:
                return
            summary = (
                f'[{NOTE_PREFIX}] Ticket {ticket_id} ("{title}", {stage_name}) has made no '
                f"progress for {minutes} min. Workers: "
                + (
                    ", ".join(f"{w.id[:8]} {'dead' if w.dead else w.status}" for w in workers)
                    or "none linked"
                )
                + ("; the loop already nudged and restarted them." if ladder.step else ".")
                + " Please check it and decide the next step."
            )
            await self._send(project.main_session_id, summary, project.user_id)
            state.escalated.add(ticket_id)
            text = f"no progress for {minutes} min; escalated to the lead orchestrator"
        ladder.step = next_step
        ladder.at = now
        ladder.ticket_mark = await self._note(project, ticket_id, f"step {next_step}/3, {text}.")
        for worker in workers:
            refreshed = await self._session(worker.id)
            ladder.worker_marks[worker.id] = refreshed.position if refreshed else worker.position

    # -- the lead ------------------------------------------------------------

    async def _check_lead(
        self, project: Any, state: ProjectState, config: Settings, now: float
    ) -> bool:
        """Recover an unresponsive lead; return whether it looks healthy."""
        from omnigent.server import project_orchestrator_rollover as rollover
        from omnigent.server.project_main_session import main_session_state

        lead = state.lead
        if rollover.rollover_in_progress(project.id):
            return False
        conversations = self._notifications.inputs.conversations
        permissions = getattr(self.app.state, "permission_store", None)
        main_state = await main_session_state(project, conversations, permissions)
        if main_state in ("absent", "missing"):
            if now - lead.at < config.step_s and lead.step:
                return False
            try:
                new_id = await rollover.start_fresh_main_session(
                    self.app,
                    project,
                    user_id=project.user_id,
                    expected_main_session_id=project.main_session_id,
                    reason="reliability",
                )
                _logger.info("Reliability: restored project %s's lead as %s", project.id, new_id)
            except Exception:
                _logger.warning("Reliability: could not restore the lead", exc_info=True)
            lead.step, lead.at, lead.session_id = LEAD_RESTART, now, None
            return False
        if main_state != "active" or project.main_session_id is None:
            return False
        view = await self._session(project.main_session_id)
        if view is None:
            return False
        waiting = view.oldest_pending_at is not None and now - view.oldest_pending_at >= (
            config.lead_response_s
        )
        unresponsive = view.dead or (waiting and view.status not in _MID_TURN)
        if lead.session_id != view.id:
            lead.step, lead.session_id = 0, view.id
        if not unresponsive:
            lead.step = 0
            return True
        if lead.step and now - lead.at < config.step_s:
            return False
        reason = "failed" if view.dead else "not picking up queued messages"
        if lead.step < LEAD_RESTART and await self._restart(view.id, project.user_id):
            lead.step, lead.at = LEAD_RESTART, now
            await self._lead_note(
                project, state, f"the lead orchestrator was {reason}; restarted it"
            )
            return False
        if lead.step < LEAD_ROLLOVER:
            scheduled = rollover.schedule_rollover(
                rollover._owner_request(self.app, uuid.uuid4().hex),
                view.id,
                user_id=project.user_id,
                notes=(
                    "Started by the reliability loop because the previous orchestrator was "
                    f"{reason}. Re-check any tickets it escalated."
                ),
                reason=f"reliability: lead {reason}",
            )
            lead.step, lead.at = LEAD_ROLLOVER, now
            if scheduled:
                await self._lead_note(
                    project, state, f"the lead orchestrator was still {reason}; rolled it over"
                )
        return False

    async def _lead_note(self, project: Any, state: ProjectState, text: str) -> None:
        """Record a lead recovery on the tickets that escalated to it."""
        _logger.info("Reliability: project %s: %s", project.id, text)
        for ticket_id in list(state.escalated):
            ladder = state.tickets.get(ticket_id)
            mark = await self._note(project, ticket_id, f"{text}; the escalation will be retried.")
            if ladder is not None:
                ladder.ticket_mark = mark
            # The recovered lead gets the escalation again on the next step.
            state.escalated.discard(ticket_id)
            if ladder is not None:
                ladder.step = RESTART

    # -- periodic review -----------------------------------------------------

    async def _maybe_review(
        self,
        project: Any,
        state: ProjectState,
        config: Settings,
        newest: int,
        lead_ok: bool,
        now: float,
    ) -> None:
        last = state.last_review_at or state.started_at
        if not lead_ok or now - last < config.review_s or newest <= state.reviewed_mark:
            return
        if project.main_session_id is None:
            return
        await self._send(
            project.main_session_id,
            f"[{NOTE_PREFIX}] Periodic review. Briefly check the board: does each open ticket's "
            "stage fit its work, is anything waiting on you (handoffs to land, verifications "
            "to arrange, final updates to write), and does the work still match what the "
            "owner asked? Fix clear drift; otherwise reply in one line.",
            project.user_id,
        )
        state.last_review_at, state.reviewed_mark = now, newest


def start(app: Any) -> ReliabilityLoop:
    loop = ReliabilityLoop(app)
    app.state.project_reliability = loop
    loop.start()
    return loop
