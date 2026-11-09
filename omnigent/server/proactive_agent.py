"""The built-in proactive agent and the hidden scheduled task that runs it.

When the deployment's ``proactive_agent`` gate is on for a user, that user
gets one ordinary ``scheduled_tasks`` row bound to the built-in agent. The row
has a fixed id, is created during the user's own ``POST /v1/sessions`` (so a
deployment whose store creates per-owner authorization does so inside that
user's request), and is hidden from every scheduled-task route. The agent's
instructions live in its bundle, so a new release changes them for every
later run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import builtin_agent_id
from omnigent.entities import ScheduledTask

_logger = logging.getLogger(__name__)

PROACTIVE_AGENT_NAME = "proactive"
PROACTIVE_AGENT_ID = builtin_agent_id(PROACTIVE_AGENT_NAME)
# Every built-in proactive agent, by name. Tasks bound to one of these ids are
# framework-owned and hidden from users.
PROACTIVE_AGENT_IDS: frozenset[str] = frozenset({PROACTIVE_AGENT_ID})

PROACTIVE_TASK_NAME = "Proactive agent"
# The agent's instructions live in its bundle; this only starts the run.
PROACTIVE_TASK_PROMPT = "Start your scheduled pass."
# Weekday mornings in the owner's timezone, when suggestions are most useful.
PROACTIVE_TASK_RRULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0"
PROACTIVE_TASK_MAX_COST_USD = 2.0
# Header a client may send on session create so the task fires in the user's timezone.
TIMEZONE_HEADER = "X-Omnigent-Timezone"
# A slow provisioning step gives up instead of holding the session create.
_PROVISION_TIMEOUT_S = 5.0
_TASK_ID_NAMESPACE = uuid.UUID("7b6c3e0a-5a7e-4b55-9a3c-2f1d6e9c8b41")


def is_proactive_task(task: ScheduledTask) -> bool:
    """Whether *task* is a framework-owned proactive task."""
    return task.agent_id in PROACTIVE_AGENT_IDS


def proactive_task_id(user_id: str | None, agent_name: str = PROACTIVE_AGENT_NAME) -> str:
    """Return the fixed task id for one owner and agent, as bare 32-char hex."""
    owner = user_id or ""
    return uuid.uuid5(_TASK_ID_NAMESPACE, f"{current_workspace_id()}:{owner}:{agent_name}").hex


def proactive_agent_available(app_state: Any) -> bool:
    """Whether the deployment's per-request gate lets the current caller use the agent.

    Evaluated in the caller's request context, or in a scheduled fire's owner
    identity scope, so a deployment gate that reads the current user sees the
    task owner. A failing gate is off.
    """
    available = getattr(app_state, "proactive_agent_available", None)
    if available is None:
        return False
    try:
        return bool(available())
    except Exception:
        _logger.warning("proactive_agent gate failed; treating as off", exc_info=True)
        return False


def request_timezone(value: str | None) -> str:
    """Return the IANA timezone a client sent, or ``"UTC"`` when absent or invalid."""
    if value:
        try:
            ZoneInfo(value)
            return value
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC"


class ProactiveProvisioner:
    """Creates each user's proactive task once per server process.

    A user counts as provisioned only after the row exists and, with the
    in-process scheduler, its timer is armed, so a failure is retried on the
    user's next session create.
    """

    def __init__(self) -> None:
        self._checked: set[tuple[int, str | None]] = set()
        self._running: set[tuple[int, str | None]] = set()

    def needed(self, user_id: str | None) -> bool:
        """Whether *user_id* is neither provisioned nor being provisioned here."""
        key = (current_workspace_id(), user_id)
        return key not in self._checked and key not in self._running

    def ensure_task(self, store: Any, user_id: str | None, timezone: str) -> ScheduledTask:
        """Return *user_id*'s proactive task, creating it if missing (database work only).

        :param store: The scheduled-task store.
        :param user_id: The owner, or ``None`` in single-user mode.
        :param timezone: IANA timezone a new schedule runs in.
        """
        task_id = proactive_task_id(user_id)
        existing = store.get(task_id)
        if existing is not None:
            return existing
        task = store.create(
            scheduled_task_id=task_id,
            name=PROACTIVE_TASK_NAME,
            prompt=PROACTIVE_TASK_PROMPT,
            rrule=PROACTIVE_TASK_RRULE,
            user_id=user_id,
            agent_id=PROACTIVE_AGENT_ID,
            timezone=timezone,
            max_cost_usd=PROACTIVE_TASK_MAX_COST_USD,
            session_mode="new",
            execution_target="managed_sandbox",
        )
        _logger.info("Provisioned proactive task %s", task.id)
        return task

    async def provision(
        self, store: Any, user_id: str | None, timezone: str, scheduler: Any | None
    ) -> ScheduledTask:
        """Ensure the row off the event loop, then arm the in-process timer on it.

        :param scheduler: Legacy in-process scheduler, or ``None`` when persistent
            dispatch picks the row up from the database.
        """
        key = (current_workspace_id(), user_id)
        self._running.add(key)
        try:
            task = await asyncio.to_thread(self.ensure_task, store, user_id, timezone)
            if scheduler is not None:
                # Re-adding replaces the timer, so a row left by a failed attempt is armed too.
                scheduler.add(task)
            self._checked.add(key)
            return task
        finally:
            self._running.discard(key)


def start_provisioning(request: Any, user_id: str | None) -> asyncio.Task[ScheduledTask] | None:
    """Start provisioning the caller's proactive task alongside a session create.

    Runs inside the request, so a store that creates per-owner authorization
    acts as the caller. Skipped when the gate is off for the caller, this
    process already provisioned the caller, or the deployment cannot launch
    managed sandboxes.
    """
    state = request.app.state
    provisioner: ProactiveProvisioner | None = getattr(state, "proactive_provisioner", None)
    store = getattr(state, "scheduled_task_store", None)
    if provisioner is None or store is None or not provisioner.needed(user_id):
        return None
    if not proactive_agent_available(state):
        return None
    deployment = getattr(state, "sandbox_config", None)
    if deployment is None or not deployment.managed_launch_supported:
        _logger.info("Proactive task not provisioned: managed sandboxes are unavailable")
        return None
    timezone = request_timezone(request.headers.get(TIMEZONE_HEADER))
    scheduler = None
    if not getattr(state, "scheduled_task_dispatch_enabled", False):
        scheduler = getattr(state, "scheduled_task_scheduler", None)
    task = asyncio.create_task(provisioner.provision(store, user_id, timezone, scheduler))
    task.add_done_callback(_log_provisioning_failure)
    return task


def _log_provisioning_failure(task: asyncio.Task[ScheduledTask]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _logger.warning("Proactive task provisioning failed", exc_info=task.exception())


async def finish_provisioning(task: asyncio.Task[ScheduledTask] | None) -> None:
    """Wait briefly for provisioning; never fail the session create.

    A failure is logged, and the caller's next session create tries again.
    """
    if task is None:
        return
    # The done callback logs failures; a slow step keeps running without the create.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(task), timeout=_PROVISION_TIMEOUT_S)


def fire_skip_reason(app_state: Any, agent_store: Any, task: ScheduledTask) -> str | None:
    """Why a proactive task should not run now, or ``None`` to run it."""
    if not proactive_agent_available(app_state):
        return "proactive agent is turned off"
    if agent_store.get(task.agent_id) is None:
        return "proactive agent is not available"
    return None
