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
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.db.account_authority import account_generation
from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import builtin_agent_id
from omnigent.entities import ScheduledTask
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.util.uuid import normalize_uuid

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
# Longer than any IANA name; a longer header is not a timezone.
_MAX_TIMEZONE_CHARS = 64
# Each account re-registration leaves one deleted task behind.
_MAX_TASK_SLOTS = 32
_ProvisionTask = asyncio.Task[ScheduledTask | None]
_TASK_ID_NAMESPACE = uuid.UUID("7b6c3e0a-5a7e-4b55-9a3c-2f1d6e9c8b41")


def reject_proactive_agent_id(agent_id: str | None) -> None:
    """Refuse a client's request to run the proactive agent outside its schedule.

    Agent lookup accepts dashed and legacy-prefixed spellings of an id, so
    those are refused too. Scheduled runs create their sessions internally.

    :raises OmnigentError: 400 when *agent_id* names the proactive agent.
    """
    if normalize_uuid(agent_id) in PROACTIVE_AGENT_IDS:
        raise OmnigentError(
            "This agent runs only on its built-in schedule", code=ErrorCode.INVALID_INPUT
        )


def is_proactive_task(task: ScheduledTask) -> bool:
    """Whether *task* is a framework-owned proactive task."""
    return task.agent_id in PROACTIVE_AGENT_IDS


def proactive_task_id(
    user_id: str | None, agent_name: str = PROACTIVE_AGENT_NAME, *, slot: int = 0
) -> str:
    """Return a fixed task id for one owner and agent, as bare 32-char hex.

    Deleting an account soft-deletes its tasks, keeping their ids, so a user who
    registers again gets their task at the next *slot*.
    """
    owner = user_id or ""
    name = f"{current_workspace_id()}:{owner}:{agent_name}"
    if slot:
        name = f"{name}:{slot}"
    return uuid.uuid5(_TASK_ID_NAMESPACE, name).hex


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


def proactive_agent_enabled_for(app_state: Any, user_id: str | None) -> bool:
    """Whether the agent is on for *user_id*: the deployment gate and both settings.

    Reads the settings store, so call it off the event loop, in the user's
    request or identity scope. A failing check is off.
    """
    enabled_for = getattr(app_state, "proactive_agent_enabled_for", None)
    if enabled_for is None:
        return False
    try:
        return bool(enabled_for(user_id or RESERVED_USER_LOCAL))
    except Exception:
        _logger.warning("proactive_agent settings check failed; treating as off", exc_info=True)
        return False


def request_timezone(value: str | None) -> str:
    """Return the IANA timezone a client sent, or ``"UTC"`` when absent or invalid."""
    if value and len(value) <= _MAX_TIMEZONE_CHARS:
        try:
            ZoneInfo(value)
            return value
        # A directory such as "America", or a name too long for the filesystem,
        # raises OSError rather than ZoneInfoNotFoundError.
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return "UTC"


_ProvisionKey = tuple[int, str | None, str | None]


def _provision_key(user_id: str | None) -> _ProvisionKey:
    # A re-registered account has a new generation, so it is provisioned again.
    generation = account_generation(user_id) if user_id is not None else None
    return (current_workspace_id(), user_id, generation)


def _free_task_id(store: Any, user_id: str | None) -> str | ScheduledTask:
    """Return the owner's live proactive task, or the id a new one should take.

    Rows soft-deleted with an earlier account registration are skipped rather
    than reactivated, so their authority never carries over.
    """
    for slot in range(_MAX_TASK_SLOTS):
        task_id = proactive_task_id(user_id, slot=slot)
        existing = store.get(task_id)
        if existing is None:
            return task_id
        if existing.state != "deleted":
            return existing
    raise RuntimeError(f"no free proactive task id after {_MAX_TASK_SLOTS} deleted tasks")


class ProactiveProvisioner:
    """Creates each user's proactive task once per server process.

    A user counts as provisioned only after the row exists and, with the
    in-process scheduler, its timer is armed, so a failure is retried on the
    user's next session create.
    """

    def __init__(self) -> None:
        self._checked: set[_ProvisionKey] = set()
        self._running: set[_ProvisionKey] = set()

    def needed(self, user_id: str | None) -> bool:
        """Whether *user_id* is neither provisioned nor being provisioned here."""
        key = _provision_key(user_id)
        return key not in self._checked and key not in self._running

    def ensure_task(
        self, store: Any, agent_store: Any, user_id: str | None, timezone: str
    ) -> ScheduledTask | None:
        """Return *user_id*'s proactive task, creating it if missing (database work only).

        :param store: The scheduled-task store.
        :param agent_store: The agent store; no task is created without the built-in agent.
        :param user_id: The owner, or ``None`` in single-user mode.
        :param timezone: IANA timezone a new schedule runs in.
        :returns: The task, or ``None`` when the built-in agent was not seeded.
        """
        if agent_store.get(PROACTIVE_AGENT_ID) is None:
            _logger.info("Proactive task not provisioned: the built-in agent is not seeded")
            return None
        task_id = _free_task_id(store, user_id)
        if not isinstance(task_id, str):
            return task_id
        # The store checks the caller's current account registration.
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
        self,
        store: Any,
        agent_store: Any,
        user_id: str | None,
        timezone: str,
        scheduler: Any | None,
        enabled: Callable[[str | None], bool] | None = None,
    ) -> ScheduledTask | None:
        """Ensure the row off the event loop, then arm the in-process timer on it.

        :param scheduler: Legacy in-process scheduler, or ``None`` when persistent
            dispatch picks the row up from the database.
        :param enabled: Whether the agent is on for the user; while it is off,
            nothing is created and the next session create checks again.
        """
        key = _provision_key(user_id)
        self._running.add(key)
        try:
            if enabled is not None and not await asyncio.to_thread(enabled, user_id):
                return None
            task = await asyncio.to_thread(self.ensure_task, store, agent_store, user_id, timezone)
            if task is not None and scheduler is not None:
                # Re-adding replaces the timer, so a row left by a failed attempt is armed too.
                scheduler.add(task)
            self._checked.add(key)
            return task
        finally:
            self._running.discard(key)


def start_provisioning(request: Any, user_id: str | None) -> _ProvisionTask | None:
    """Start provisioning the caller's proactive task alongside a session create.

    Runs inside the request, so a store that creates per-owner authorization
    acts as the caller. Skipped when the deployment gate or either setting is
    off for the caller, this process already provisioned the caller, or the
    deployment cannot launch managed sandboxes.
    """
    try:
        return _start_provisioning(request, user_id)
    except Exception:
        _logger.warning("Proactive task provisioning could not start", exc_info=True)
        return None


def _start_provisioning(request: Any, user_id: str | None) -> _ProvisionTask | None:
    state = request.app.state
    provisioner: ProactiveProvisioner | None = getattr(state, "proactive_provisioner", None)
    store = getattr(state, "scheduled_task_store", None)
    agent_store = getattr(state, "agent_store", None)
    if provisioner is None or store is None or agent_store is None:
        return None
    if not provisioner.needed(user_id):
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
    task = asyncio.create_task(
        provisioner.provision(
            store,
            agent_store,
            user_id,
            timezone,
            scheduler,
            enabled=lambda owner: proactive_agent_enabled_for(state, owner),
        )
    )
    task.add_done_callback(_log_provisioning_failure)
    return task


def _log_provisioning_failure(task: _ProvisionTask) -> None:
    if not task.cancelled() and task.exception() is not None:
        _logger.warning("Proactive task provisioning failed", exc_info=task.exception())


async def finish_provisioning(task: _ProvisionTask | None) -> None:
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
    if not proactive_agent_enabled_for(app_state, task.user_id):
        return "proactive agent is turned off for this user"
    if agent_store.get(task.agent_id) is None:
        return "proactive agent is not available"
    return None
