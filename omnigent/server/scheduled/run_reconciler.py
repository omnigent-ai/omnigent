"""Scheduled-task run reconciler — the run-lifecycle completion backstop.

The fire path (:mod:`omnigent.server.scheduled.fire`) creates a conversation,
dispatches the prompt, and records a ``scheduled_task_runs`` row as
``running`` — then returns immediately, WITHOUT waiting for the agent turn to
finish. Nothing in that path ever revisits the row, so historically a run
stayed ``running`` with ``finished_at = NULL`` forever, even after the agent
completed.

There is no durable completion callback to hang the transition on: the
pub-sub event bus (:mod:`omnigent.runtime.session_stream`) fans terminal
``response.*`` events only to live SSE subscribers with no durable buffer, so
a scheduled fire (which has no attached client) would miss them, and any
in-memory listener would not survive a server restart.

This module closes the gap with a **periodic reconciliation sweep**. Every
:data:`RECONCILE_INTERVAL_SECONDS` it lists every still-``running`` run across
all workspaces and, for each, reads the DURABLE state its conversation left
behind (see :func:`classify_conversation_terminal_state`):

- the conversation's ``live_status`` (relay-persisted; a cheap pre-filter), and
- the conversation's committed items + failure labels (the authoritative
  transcript, which survives with no client attached and across restarts).

A conversation whose turn has completed flips the run to ``succeeded``; an
errored/cancelled turn flips it to ``failed`` with an ``error_code``. A run
that has been ``running`` longer than :data:`STALE_RUN_MAX_AGE_SECONDS`
without reaching a terminal conversation state is force-failed with
``error_code = "incomplete"`` (the host-died-mid-turn case) so the invariant
"every run eventually reaches a terminal state" always holds. The store's
:meth:`update_run` is conditional (``WHERE status = running``) and idempotent,
so a run already terminal (a fire-time ``skipped``/``failed``, or a prior
sweep) is never clobbered and two sweeps cannot double-transition.

A lower-latency relay hook (transition the run the instant the relay observes
``response.completed``) is a possible future optimization; it is deliberately
NOT part of this backstop, which must work without any live subscription.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.db.db_models import workspace_scope
from omnigent.server.routes.sessions import (
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
)

if TYPE_CHECKING:
    from omnigent.entities import Conversation, ScheduledTaskRun
    from omnigent.stores import ConversationStore
    from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# How often the sweep runs. The RRULE floor is 1 hour, so runs are not
# high-frequency; 60s is responsive without churning the DB. Module constant
# so it stays tunable.
RECONCILE_INTERVAL_SECONDS: int = 60

# A run still ``running`` longer than this — with no terminal conversation
# state — is force-failed with ``error_code = "incomplete"`` (host died
# mid-turn, runner never reported completion). Deliberately generous (6h) so a
# legitimately long agent turn is never killed; a stuck-``running`` row is a
# far milder bug than a falsely-``failed`` one.
STALE_RUN_MAX_AGE_SECONDS: int = 6 * 60 * 60

# error_code recorded on the stale-run force-fail path.
STALE_RUN_ERROR_CODE: str = "incomplete"

# Conversation ``live_status`` values that mean the turn is still in flight —
# used as a cheap pre-filter to skip the items read for clearly-running runs.
_IN_FLIGHT_LIVE_STATUSES: frozenset[str] = frozenset({"running", "waiting"})


@dataclass(frozen=True)
class _TerminalDecision:
    """Outcome of classifying a run's conversation.

    :param status: Terminal run status to set (``"succeeded"`` / ``"failed"``),
        or ``None`` to leave the run ``running`` (turn still in flight).
    :param error_code: Failure classification when ``status == "failed"``.
    :param error: Human-readable failure detail when ``status == "failed"``.
    """

    status: str | None
    error_code: str | None = None
    error: str | None = None


def _latest_message_completed(conversation_store: ConversationStore, conversation_id: str) -> bool:
    """Return whether the conversation's newest message item is ``completed``.

    The relay commits assistant output as ``message`` items; the newest one
    reaching ``status == "completed"`` is the durable, client-independent
    signal that the dispatched turn produced a finished response.

    :param conversation_store: Store to read committed items from.
    :param conversation_id: The fired conversation to inspect.
    :returns: ``True`` if the latest ``message`` item is ``completed``.
    """
    page = conversation_store.list_items(
        conversation_id,
        limit=1,
        order="desc",
        type="message",
    )
    if not page.data:
        return False
    return page.data[0].status == "completed"


def classify_conversation_terminal_state(
    conversation: Conversation | None,
    conversation_store: ConversationStore,
    conversation_id: str,
) -> _TerminalDecision:
    """Decide the terminal run status for a ``running`` run's conversation.

    Read order (durable signals only — no live subscription required):

    #. **Missing conversation** — the fired conversation was deleted. Treat as
       terminal ``failed`` (``conversation_missing``); nothing left to await.
    #. **Failure labels** — a ``session.status: failed`` edge persists
       ``omnigent.last_task_error_code``; a non-empty value means the turn
       terminally errored → ``failed`` carrying that code.
    #. **live_status pre-filter** — ``running``/``waiting`` means still in
       flight; leave it (unless stale, handled by the caller).
    #. **Completed transcript** — the newest ``message`` item is ``completed``
       → ``succeeded``.
    #. **Otherwise** — not yet terminal; leave ``running`` (the caller applies
       the stale-age backstop).

    :param conversation: The fired conversation, or ``None`` if it no longer
        exists.
    :param conversation_store: Store used to read committed items.
    :param conversation_id: The fired conversation id (for the items read).
    :returns: A :class:`_TerminalDecision`.
    """
    if conversation is None:
        return _TerminalDecision(
            status="failed",
            error_code="conversation_missing",
            error="scheduled run conversation no longer exists",
        )

    error_code = (conversation.labels or {}).get(_LAST_TASK_ERROR_CODE_LABEL_KEY)
    if error_code:
        return _TerminalDecision(
            status="failed",
            error_code=error_code,
            error="scheduled run turn reported a terminal error",
        )

    if conversation.live_status in _IN_FLIGHT_LIVE_STATUSES:
        return _TerminalDecision(status=None)

    if _latest_message_completed(conversation_store, conversation_id):
        return _TerminalDecision(status="succeeded")

    return _TerminalDecision(status=None)


class ScheduledRunReconciler:
    """Periodic backstop that transitions ``running`` runs to terminal states.

    Owns its own asyncio loop, kept OFF the :class:`ScheduledTaskScheduler`
    (which is purely per-job timers) so the scan responsibility stays
    separate. Wire :meth:`start` / :meth:`stop` into the server lifespan next
    to the scheduler.
    """

    def __init__(
        self,
        scheduled_task_store: ScheduledTaskStore,
        conversation_store: ConversationStore,
        *,
        interval_seconds: int = RECONCILE_INTERVAL_SECONDS,
        stale_max_age_seconds: int = STALE_RUN_MAX_AGE_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._scheduled_task_store = scheduled_task_store
        self._conversation_store = conversation_store
        self._interval = interval_seconds
        self._stale_max_age = stale_max_age_seconds
        self._now = now
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the periodic sweep loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.ensure_future(self._loop())
        _logger.info(
            "ScheduledRunReconciler started (interval=%ds, stale_max_age=%ds)",
            self._interval,
            self._stale_max_age,
        )

    def stop(self) -> None:
        """Cancel the sweep loop. Idempotent."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        """Run :meth:`reconcile_once` every ``interval`` seconds until cancelled."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A sweep failure must not kill the loop.
                _logger.exception("ScheduledRunReconciler sweep failed; continuing")

    async def reconcile_once(self) -> int:
        """Run a single sweep over all ``running`` runs across every workspace.

        :returns: The number of runs transitioned to a terminal state.
        """
        runs = await asyncio.to_thread(
            self._scheduled_task_store.list_runs_by_status_all_workspaces, "running"
        )
        transitioned = 0
        for run in runs:
            try:
                if await self._reconcile_run(run):
                    transitioned += 1
            except Exception:
                # One bad run must not abort the sweep.
                _logger.exception("ScheduledRunReconciler failed to reconcile run %s", run.id)
        return transitioned

    async def _reconcile_run(self, run: ScheduledTaskRun) -> bool:
        """Reconcile one ``running`` run against its conversation.

        Runs inside the run's ``workspace_scope`` so every store query filters
        on the owning workspace (multi-tenant), mirroring the fire path.

        :param run: A run currently in ``running``.
        :returns: ``True`` if the run was transitioned to a terminal state.
        """
        with workspace_scope(run.workspace_id):
            now = int(self._now())
            age = now - run.scheduled_at

            conversation_id = run.conversation_id
            decision = _TerminalDecision(status=None)
            if conversation_id is not None:
                conversation = await asyncio.to_thread(
                    self._conversation_store.get_conversation, conversation_id
                )
                decision = classify_conversation_terminal_state(
                    conversation,
                    self._conversation_store,
                    conversation_id,
                )

            if decision.status is None:
                # Not yet terminal. Force-fail only if the run is stale — a
                # host that died mid-turn will never report completion.
                if age < self._stale_max_age:
                    return False
                decision = _TerminalDecision(
                    status="failed",
                    error_code=STALE_RUN_ERROR_CODE,
                    error=(
                        "scheduled run did not reach a terminal state within "
                        f"{self._stale_max_age}s"
                    ),
                )

            updated = await asyncio.to_thread(
                self._scheduled_task_store.update_run,
                run.id,
                status=decision.status,
                finished_at=now,
                error=decision.error,
                error_code=decision.error_code,
            )
            if updated is None:
                # Already terminal (a concurrent sweep or fire-time write won).
                return False
            _logger.info(
                "ScheduledRunReconciler: run %s (task %s) %s -> %s%s",
                run.id,
                run.scheduled_task_id,
                "running",
                decision.status,
                f" ({decision.error_code})" if decision.error_code else "",
            )
            return True
