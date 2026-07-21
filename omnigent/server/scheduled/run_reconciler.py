"""Scheduled-task run-completion backstop — startup sweep + lazy-on-read.

The fire path (:mod:`omnigent.server.scheduled.fire`) creates a conversation,
dispatches the prompt, and records a ``scheduled_task_runs`` row as
``running`` — then returns immediately, WITHOUT waiting for the agent turn to
finish.

**The PRIMARY completion mechanism is event-driven** and lives elsewhere:
:func:`omnigent.server.session_live_state.persist_scheduled_run_completion`,
fired from ``_publish_status`` the instant a fired conversation's turn reaches
a terminal edge, flips the run ``running`` → ``succeeded``/``failed``. That
handler rides the same long-lived SSE relay that already persists the
conversation's ``live_status`` for a browserless scheduled fire, so it needs
no live client and no periodic poll.

This module is only the **orphan backstop** for the case the event path cannot
cover: a run left ``running`` because the terminal event never fired or was
lost (host died mid-turn, or the server restarted while a fire was in flight).
It enforces the invariant "every run eventually reaches a terminal state"
WITHOUT a recurring background poll, via two cheap catches:

- :meth:`ScheduledRunReconciler.run_startup_sweep` — runs ONCE per server boot
  (wired into the lifespan). It lists every still-``running`` run across all
  workspaces and force-fails those whose conversation is already terminal /
  missing, or that are past :data:`STALE_RUN_MAX_AGE_SECONDS`. This catches
  runs orphaned by a restart mid-fire.
- lazy-on-read at ``GET /v1/scheduled-tasks/{id}/runs`` (in the route, not
  here) — force-fails a task's runs still ``running`` past the max age when
  run history is read.

Both reuse :func:`classify_conversation_terminal_state` (reads the durable
``live_status`` + committed transcript + failure labels the conversation left
behind) and the idempotent, conditional :meth:`update_run`
(``WHERE status = running``), so a run already terminal — via the event hook, a
fire-time ``skipped``/``failed``, or the other backstop — is never clobbered.

There is deliberately NO periodic sweep of any cadence: the event hook makes
one unnecessary, and startup-sweep + lazy-on-read match how the sibling
scheduled-task systems reconcile orphans (at a lifecycle boundary, not on a
timer).
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

# A run still ``running`` longer than this — with no terminal conversation
# state — is force-failed with ``error_code = "incomplete"`` (host died
# mid-turn, runner never reported completion). Deliberately generous (6h) so a
# legitimately long agent turn is never killed; a stuck-``running`` row is a
# far milder bug than a falsely-``failed`` one. Shared by the startup sweep and
# the lazy-on-read backstop.
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
    """Orphan backstop that transitions stranded ``running`` runs to terminal.

    NOT a periodic poll — the primary completion path is the event hook
    (:func:`omnigent.server.session_live_state.persist_scheduled_run_completion`).
    This runs its sweep ONCE per boot via :meth:`run_startup_sweep` (wired into
    the server lifespan) to catch runs orphaned by a restart mid-fire; the
    lazy-on-read backstop in the runs route covers the rest.
    """

    def __init__(
        self,
        scheduled_task_store: ScheduledTaskStore,
        conversation_store: ConversationStore,
        *,
        stale_max_age_seconds: int = STALE_RUN_MAX_AGE_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._scheduled_task_store = scheduled_task_store
        self._conversation_store = conversation_store
        self._stale_max_age = stale_max_age_seconds
        self._now = now

    async def run_startup_sweep(self) -> int:
        """Reconcile all ``running`` runs once at server startup.

        Catches runs orphaned by a server restart that happened while a fire
        was in flight (the terminal event fired on the old process, or never).
        A run whose conversation is already terminal/missing is transitioned to
        the mapped status; one past the max age with no terminal state is
        force-failed ``incomplete``; a young in-flight run is left alone for the
        event hook to complete.

        :returns: The number of runs transitioned to a terminal state.
        """
        transitioned = await self._sweep_running_runs()
        _logger.info(
            "ScheduledRunReconciler startup sweep: transitioned %d orphaned run(s)",
            transitioned,
        )
        return transitioned

    async def _sweep_running_runs(self) -> int:
        """List all ``running`` runs across workspaces and reconcile each.

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
