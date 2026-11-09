"""The built-in proactive agent and the hidden scheduled task that runs it.

When the ``proactive_agent`` feature is on and an admin enables the agent,
each user gets one ordinary ``scheduled_tasks`` row bound to the built-in
agent. The row has a fixed id, is created during the user's own
``POST /v1/sessions`` (so a deployment whose store creates per-owner
authorization does so inside that user's request), and is hidden from every
scheduled-task route. The agent's instructions live in its bundle, so a new
release changes them for every later run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import builtin_agent_id
from omnigent.entities import ScheduledTask
from omnigent.server.admin_list import resolve_data_dir
from omnigent.server.feature_flags import Feature
from omnigent.server.sharing_settings import _read_override_text, _write_override_text

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
_SETTINGS_FILE = "proactive_agents"


def is_proactive_task(task: ScheduledTask) -> bool:
    """Whether *task* is a framework-owned proactive task."""
    return task.agent_id in PROACTIVE_AGENT_IDS


def proactive_task_id(user_id: str | None, agent_name: str = PROACTIVE_AGENT_NAME) -> str:
    """Return the fixed task id for one owner and agent, as bare 32-char hex."""
    owner = user_id or ""
    return uuid.uuid5(_TASK_ID_NAMESPACE, f"{current_workspace_id()}:{owner}:{agent_name}").hex


@dataclass(frozen=True)
class ProactiveAgentState:
    """Admin settings for proactive agents.

    :param enabled: Whether proactive agents run at all.
    :param agents: Per-agent override by name; an agent not listed follows
        ``enabled``.
    """

    enabled: bool = False
    agents: dict[str, bool] = field(default_factory=dict)

    def agent_enabled(self, name: str) -> bool:
        """Whether *name* runs under these settings."""
        return self.enabled and self.agents.get(name, True)


class ProactiveAgentSettings(ABC):
    """Where the admin's proactive-agent settings are stored."""

    #: Whether admins can change the settings through the API.
    editable: bool = True

    @abstractmethod
    def read(self) -> ProactiveAgentState:
        """Return the current settings."""

    @abstractmethod
    def write(self, state: ProactiveAgentState) -> None:
        """Persist *state*."""


class FileProactiveAgentSettings(ProactiveAgentSettings):
    """Settings stored as JSON in ``<data_dir>/proactive_agents``; off when absent."""

    def __init__(self, path: Path | None = None) -> None:
        """:param path: Settings file; defaults to the server data dir."""
        self._path = path

    def _file(self) -> Path:
        return self._path or resolve_data_dir() / _SETTINGS_FILE

    def read(self) -> ProactiveAgentState:
        """See base class. An unreadable or invalid file reads as off."""
        raw = _read_override_text(self._file())
        if not raw:
            return ProactiveAgentState()
        try:
            data = json.loads(raw)
            agents = {str(k): bool(v) for k, v in dict(data.get("agents") or {}).items()}
            return ProactiveAgentState(enabled=data.get("enabled") is True, agents=agents)
        except (ValueError, TypeError, AttributeError):
            _logger.warning("Ignoring invalid proactive agent settings in %s", self._file())
            return ProactiveAgentState()

    def write(self, state: ProactiveAgentState) -> None:
        """See base class."""
        payload = {"enabled": state.enabled, "agents": state.agents}
        _write_override_text(self._file(), json.dumps(payload, sort_keys=True))


def proactive_agent_runs(app_state: Any, agent_name: str = PROACTIVE_AGENT_NAME) -> bool:
    """Whether the deployment and the admin settings let *agent_name* run."""
    flags = getattr(app_state, "feature_flags", None)
    settings: ProactiveAgentSettings | None = getattr(app_state, "proactive_agent_settings", None)
    if flags is None or settings is None or not flags.enabled(Feature.PROACTIVE_AGENT):
        return False
    try:
        return settings.read().agent_enabled(agent_name)
    except Exception:
        _logger.warning("Cannot read proactive agent settings; treating as off", exc_info=True)
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
    """Creates each user's proactive task once per server process."""

    def __init__(self) -> None:
        self._checked: set[tuple[int, str | None]] = set()

    def needed(self, user_id: str | None) -> bool:
        """Whether this process has not yet provisioned *user_id*."""
        return (current_workspace_id(), user_id) not in self._checked

    def provision(
        self, store: Any, user_id: str | None, timezone: str, scheduler: Any | None
    ) -> ScheduledTask | None:
        """Create *user_id*'s proactive task if it is missing.

        :param store: The scheduled-task store.
        :param user_id: The owner, or ``None`` in single-user mode.
        :param timezone: IANA timezone the schedule runs in.
        :param scheduler: Legacy in-process scheduler to arm, or ``None``.
        :returns: The created task, or ``None`` when it already existed.
        """
        task_id = proactive_task_id(user_id)
        if store.get(task_id) is not None:
            self._checked.add((current_workspace_id(), user_id))
            return None
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
        self._checked.add((current_workspace_id(), user_id))
        if scheduler is not None:
            scheduler.add(task)
        _logger.info("Provisioned proactive task %s", task.id)
        return task


def start_provisioning(
    request: Any, user_id: str | None
) -> asyncio.Task[ScheduledTask | None] | None:
    """Start provisioning the caller's proactive task alongside a session create.

    Runs inside the request, so a store that creates per-owner authorization
    acts as the caller. Skipped when the agent is off, this process already
    checked the caller, or the deployment cannot launch managed sandboxes.
    """
    state = request.app.state
    provisioner: ProactiveProvisioner | None = getattr(state, "proactive_provisioner", None)
    store = getattr(state, "scheduled_task_store", None)
    if provisioner is None or store is None or not provisioner.needed(user_id):
        return None
    if not proactive_agent_runs(state):
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
        asyncio.to_thread(provisioner.provision, store, user_id, timezone, scheduler)
    )
    task.add_done_callback(_log_provisioning_failure)
    return task


def _log_provisioning_failure(task: asyncio.Task[ScheduledTask | None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _logger.warning("Proactive task provisioning failed", exc_info=task.exception())


async def finish_provisioning(task: asyncio.Task[ScheduledTask | None] | None) -> None:
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
    if not proactive_agent_runs(app_state):
        return "proactive agent is turned off"
    if agent_store.get(task.agent_id) is None:
        return "proactive agent is not available"
    return None
