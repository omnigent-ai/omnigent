"""Provision the Slack watcher automation for a project's channel link.

The watcher runs as a scheduled task on a managed Omnigent server's
scheduled-task API (``POST {automation_url}/v1/scheduled-tasks``), usually in
another workspace, owned by the project owner. This server creates, updates, pauses and
deletes it, so people manage everything from Project settings and never edit the
automation. The calls go through the project Lead's host, signed there with the
owner's own Databricks login (see :mod:`omnigent.host.automation_requests`), so
the server holds no credential for that workspace. When the Lead's host isn't
connected, the owner's server-side Databricks connection is used if it is for
that workspace.

Each provisioning mints a fresh per-link relay key, stores only its hash, and
writes the key into the automation's instructions; the watcher presents it on
every relay call. Each link names its automation API URL in Project settings;
the relay URL is the address the owner saved the link from, so no server
configuration is needed. The app's environment can set defaults (see
:meth:`SlackAutomationConfig.from_env`).
A link with no automation URL stays ``claimed`` and nothing is provisioned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from omnigent.server.slack_watcher_prompt import watcher_prompt

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.stores.project_slack_store import ProjectSlackStore, SlackBinding

_logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 30.0
_ACTIVE = "active"
_PAUSED = "paused"
# Marks automations this server manages, so overlap checks can tell them apart.
AUTOMATION_NAME_PREFIX = "[omnigent-slack]"
# The managed server's packaged agent the watcher runs as, unless a link names another.
DEFAULT_AGENT = "claude-native-ui"
# Runs in a fresh server-provisioned sandbox, not on any person's host.
EXECUTION_TARGET = "managed_sandbox"
# Every run continues one session, which the managed API allows every ten minutes.
SESSION_MODE = "continue"
DEFAULT_RRULE = "FREQ=MINUTELY;INTERVAL=10"
# The MCP service that gives the watcher's sandbox Slack, unless a link names others.
DEFAULT_MCP_SERVICES = ("slack",)
_AGENT_ID = re.compile(r"^[0-9a-f]{32}$")
_CRON_MINUTES = re.compile(r"^\*/([0-9]{1,2}) \* \* \* \*$")


def relay_key_hash(relay_key: str) -> str:
    return hashlib.sha256(relay_key.encode()).hexdigest()


def _host(url: str) -> str:
    return (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()


def schedule_rrule(schedule: str) -> str:
    """An RRULE for *schedule*, which may also be a ``*/N * * * *`` cron expression."""
    schedule = schedule.strip()
    if match := _CRON_MINUTES.match(schedule):
        return f"FREQ=MINUTELY;INTERVAL={int(match.group(1))}"
    return schedule.upper().removeprefix("RRULE:") or DEFAULT_RRULE


def agent_id_for(agent: str) -> str:
    """The managed server's id for *agent*: an id as given, or a packaged agent's name."""
    from omnigent.db.utils import builtin_agent_id

    agent = agent.strip()
    return agent if _AGENT_ID.match(agent) else builtin_agent_id(agent or DEFAULT_AGENT)


@dataclass(frozen=True)
class SlackAutomationConfig:
    """Defaults for where watcher automations run and how they call back.

    A link's own ``automation_url``, ``agent``, ``model`` and ``mcp_services``
    take precedence; ``relay_url`` here overrides the address a link was saved from.

    :param api_url: Base URL of the managed Omnigent API, e.g.
        ``https://automation.example.com/api/2.0/omnigent``, or ``""`` for none.
    :param relay_url: This server's public URL, which the watcher calls, or ``""``.
    :param agent: The agent the watcher runs as: a packaged agent's name or an id.
    :param model: Model override, or ``""`` for the agent's default.
    :param rrule: How often the watcher polls the channel.
    :param mcp_services: MCP services the watcher's sandbox starts with.
    """

    api_url: str = ""
    relay_url: str = ""
    agent: str = DEFAULT_AGENT
    model: str = ""
    rrule: str = DEFAULT_RRULE
    mcp_services: tuple[str, ...] = DEFAULT_MCP_SERVICES

    def api_url_for(self, binding: SlackBinding) -> str:
        return ((binding.destination or {}).get("automation_url") or self.api_url).rstrip("/")

    def relay_url_for(self, binding: SlackBinding) -> str:
        return (self.relay_url or (binding.destination or {}).get("relay_url") or "").rstrip("/")

    @classmethod
    def from_env(cls) -> SlackAutomationConfig:
        """Defaults from the app's environment; every setting is optional."""
        relay_url = os.environ.get("OMNIGENT_SLACK_RELAY_URL", "").strip().rstrip("/")
        if not relay_url:
            domain = os.environ.get("OMNIGENT_DOMAIN", "").strip()
            relay_url = f"https://{domain}" if domain else ""
        services = os.environ.get("OMNIGENT_SLACK_AUTOMATION_MCP_SERVICES")
        return cls(
            api_url=os.environ.get("OMNIGENT_SLACK_AUTOMATION_API_URL", "").strip().rstrip("/"),
            relay_url=relay_url,
            agent=os.environ.get("OMNIGENT_SLACK_AUTOMATION_AGENT", "").strip() or DEFAULT_AGENT,
            model=os.environ.get("OMNIGENT_SLACK_AUTOMATION_MODEL", "").strip(),
            rrule=schedule_rrule(os.environ.get("OMNIGENT_SLACK_AUTOMATION_SCHEDULE", "")),
            mcp_services=(
                tuple(name.strip() for name in services.split(",") if name.strip())
                if services is not None
                else DEFAULT_MCP_SERVICES
            ),
        )


class AutomationAuthError(Exception):
    """The owner's connection was refused; they need to reconnect."""


class AutomationError(Exception):
    """The automation service refused or failed a request."""


# How long the Lead's host may take to sign and make one call.
_HOST_CALL_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ViaHost:
    """Make a call on the owner's connected host, signed with the owner's login there."""

    registry: Any
    connection: Any

    async def send(self, method: str, url: str, body: dict[str, Any] | None) -> tuple[int, str]:
        from omnigent.host.frames import HostAutomationRequestFrame, encode_host_frame

        request_id = secrets.token_hex(8)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.connection.pending_automation_requests[request_id] = future
        frame = HostAutomationRequestFrame(
            request_id=request_id, method=method, url=url, body=body
        )
        try:
            try:
                self.registry.send_text(self.connection, encode_host_frame(frame))
            except ConnectionError as exc:
                raise AutomationError("The project Lead's host went offline; retry") from exc
            try:
                result = await asyncio.wait_for(future, timeout=_HOST_CALL_TIMEOUT_S)
            except TimeoutError as exc:
                raise AutomationError(
                    "The project Lead's host didn't answer in time; retry"
                ) from exc
        finally:
            self.connection.pending_automation_requests.pop(request_id, None)
        if result.get("error"):
            error = str(result["error"])
            if "login" in error.lower():
                raise AutomationAuthError(error)
            raise AutomationError(f"The project Lead's host refused the call: {error}")
        return int(result.get("status_code") or 0), str(result.get("body") or "")


class AutomationNotFoundError(AutomationError):
    """The automation is gone, for example deleted by hand."""


def _error_detail(text: str) -> str:
    """The managed API's reason for refusing a call, e.g. a validation message."""
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:300]
    if isinstance(payload, dict):
        detail = payload.get("detail", payload.get("error", payload))
        if isinstance(detail, dict):
            detail = detail.get("message") or detail
        if isinstance(detail, list):
            detail = "; ".join(
                f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
                if isinstance(item, dict)
                else str(item)
                for item in detail
            )
        return str(detail)[:300]
    return text[:300]


class AutomationClient:
    """The managed Omnigent scheduled-task API, as the bearer's owner.

    Only five calls are made, all on ``{api_url}/v1/scheduled-tasks``: create,
    list, update (including pause and resume), and delete.
    """

    def __init__(self, api_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = f"{api_url.rstrip('/')}/v1/scheduled-tasks"
        self._transport = transport

    async def _call(
        self,
        method: str,
        path: str,
        token: str | ViaHost,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One call, made directly with a bearer *token* or on the owner's host."""
        url = f"{self._base}{path}"
        if isinstance(token, ViaHost):
            status, text = await token.send(method, url, body)
        else:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_S, transport=self._transport
            ) as http:
                try:
                    response = await http.request(
                        method, url, json=body, headers={"Authorization": f"Bearer {token}"}
                    )
                except httpx.HTTPError as exc:
                    raise AutomationError(f"The automation service is unreachable: {exc}") from exc
            status, text = response.status_code, response.text
        if status in (401, 403):
            raise AutomationAuthError(
                f"The automation service refused the owner's login ({status}): "
                f"{_error_detail(text)}. Sign in again, then retry"
            )
        if status == 404:
            if method == "DELETE":
                return {}
            raise AutomationNotFoundError("The automation no longer exists")
        if status >= 400:
            raise AutomationError(
                f"The automation service refused the watcher ({status}): {_error_detail(text)}"
            )
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise AutomationError("The automation service returned a non-JSON answer") from exc

    async def create(self, token: str | ViaHost, task: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "", token, task)

    async def update(
        self, token: str | ViaHost, task_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._call("PATCH", f"/{task_id}", token, fields)

    async def set_state(self, token: str | ViaHost, task_id: str, state: str) -> dict[str, Any]:
        return await self.update(token, task_id, {"state": state})

    async def delete(self, token: str | ViaHost, task_id: str) -> None:
        await self._call("DELETE", f"/{task_id}", token)

    async def list(self, token: str | ViaHost) -> list[dict[str, Any]]:
        page = await self._call("GET", "", token)
        return list(page.get("scheduled_tasks") or [])


def _task_id(automation_id: str) -> str:
    """The managed task's id; links set up by earlier versions stored ``owner/id``."""
    return automation_id.rpartition("/")[2]


def relay_marker(binding: SlackBinding) -> str:
    """Text every watcher prompt for *binding* contains, so a lost create is found again."""
    return f"/v1/slack-relay/{binding.id}"


class SlackProvisioner:
    """Keeps each link's watcher automation in step with Project settings."""

    def __init__(
        self,
        config: SlackAutomationConfig,
        store: ProjectSlackStore,
        *,
        connection_store: Any,
        connection_client: Any,
        client: AutomationClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        host_registry: Any = None,
        conversations: Any = None,
    ) -> None:
        self.config = config
        self._store = store
        self._connections = connection_store
        self._connection_client = connection_client
        self._fixed_client = client
        self._transport = transport
        self._host_registry = host_registry
        self._conversations = conversations

    def _client(self, binding: SlackBinding) -> AutomationClient:
        if self._fixed_client is not None:
            return self._fixed_client
        return AutomationClient(self.config.api_url_for(binding), transport=self._transport)

    async def _lead_host(self, project: Project, owner_user_id: str) -> ViaHost | None:
        """The Lead's host, when it is connected and is the owner's own host."""
        if self._host_registry is None or self._conversations is None:
            return None
        if not project.main_session_id:
            return None
        lead = await asyncio.to_thread(
            self._conversations.get_conversation, project.main_session_id
        )
        host_id = getattr(lead, "host_id", None)
        if not host_id:
            return None
        connection = self._host_registry.get(host_id)
        if connection is None or connection.owner != owner_user_id:
            return None
        return ViaHost(self._host_registry, connection)

    async def _credential(self, project: Project, binding: SlackBinding) -> str | ViaHost:
        """How to reach the automation API as the owner: their Lead's host, else a token.

        :raises AutomationAuthError: neither is available.
        """
        via_host = await self._lead_host(project, binding.owner_user_id)
        if via_host is not None:
            return via_host
        return await self._owner_token(
            binding.owner_user_id, _host(self.config.api_url_for(binding))
        )

    async def _owner_token(self, owner_user_id: str, automation_host: str) -> str:
        """The owner's server-side access token for the automation workspace.

        :raises AutomationAuthError: not connected, connected elsewhere, or expired.
        """
        from omnigent.server.databricks_identity import resolve_databricks_token

        resolved = None
        if self._connections is not None and self._connection_client is not None:
            resolved = await resolve_databricks_token(
                owner_user_id, store=self._connections, client=self._connection_client
            )
        if resolved is None:
            raise AutomationAuthError(
                "The project Lead's host isn't connected, so the channel watcher can't be set "
                "up. Start the Lead (or its host) and retry; or connect Databricks to "
                f"{automation_host} under Settings → Connections"
            )
        token, workspace_host = resolved
        if _host(workspace_host) != automation_host:
            raise AutomationAuthError(
                f"The project owner's Databricks connection is for {_host(workspace_host)}, "
                f"not {automation_host}; reconnect it there, then retry"
            )
        return token

    def _task(self, project: Project, binding: SlackBinding, relay_key: str) -> dict[str, Any]:
        """The managed API's flat create/update fields for this link's watcher."""
        destination = binding.destination or {}
        services = destination.get("mcp_services")
        label = f"#{binding.channel_name}" if binding.channel_name else binding.channel_id
        task: dict[str, Any] = {
            "name": f"{AUTOMATION_NAME_PREFIX} {project.name} ← {label}",
            "prompt": watcher_prompt(
                project_name=project.name,
                binding=binding,
                relay_url=self.config.relay_url_for(binding),
                relay_key=relay_key,
            ),
            "rrule": self.config.rrule,
            "agent_id": agent_id_for(destination.get("agent") or self.config.agent),
            "timezone": "UTC",
            "execution_target": EXECUTION_TARGET,
            "session_mode": SESSION_MODE,
            "mcp_services": (
                [name.strip() for name in services.split(",") if name.strip()]
                if services is not None
                else list(self.config.mcp_services)
            ),
        }
        model = destination.get("model") or self.config.model
        if model:
            task["model_override"] = model
        return task

    async def _find(
        self, client: AutomationClient, token: str | ViaHost, binding: SlackBinding
    ) -> str | None:
        """The id of a watcher an earlier, interrupted setup already created."""
        marker = relay_marker(binding)
        for task in await client.list(token):
            if marker in str(task.get("prompt") or "") and task.get("id"):
                return str(task["id"])
        return None

    async def _fail(
        self,
        project_id: str,
        exc: Exception,
        *,
        actor_user_id: str | None,
        keep: str | None = None,
    ) -> SlackBinding:
        state = "needs_reconnect" if isinstance(exc, AutomationAuthError) else "error"
        return await asyncio.to_thread(
            self._store.set_state,
            project_id,
            keep or state,
            actor_user_id=actor_user_id,
            last_error=str(exc)[:1000],
            action=f"provision_{state}",
        )

    async def provision(
        self, project: Project, binding: SlackBinding, *, actor_user_id: str | None
    ) -> SlackBinding:
        """Create or update the watcher and switch it on; the link ends ``active``.

        Retrying is safe: creation is keyed on the link's id, so a lost response
        can't leave two watchers.
        """
        waiting = None
        if not self.config.api_url_for(binding):
            waiting = "The project owner needs to add the automation API URL in Slack settings"
        elif not self.config.relay_url_for(binding):
            waiting = "The project owner needs to save this link once to set where it calls back"
        if waiting is not None:
            return await asyncio.to_thread(
                self._store.set_state,
                project.id,
                "claimed",
                actor_user_id=actor_user_id,
                last_error=waiting,
                action="provision_waiting",
            )
        relay_key = secrets.token_urlsafe(32)
        client = self._client(binding)
        try:
            token = await self._credential(project, binding)
            task = self._task(project, binding, relay_key)
            # No idempotency key on create: find a watcher a lost answer left behind.
            task_id = _task_id(binding.automation_id or "") or await self._find(
                client, token, binding
            )
            result: dict[str, Any] | None = None
            if task_id:
                try:
                    result = await client.update(token, task_id, {**task, "state": _ACTIVE})
                except AutomationNotFoundError:
                    task_id = None
            if not task_id:
                result = await client.create(token, task)
                task_id = str(result.get("id") or "")
                if not task_id:
                    raise AutomationError("The automation service didn't return the new task's id")
                if result.get("state") != _ACTIVE:
                    result = await client.set_state(token, task_id, _ACTIVE)
        except (AutomationAuthError, AutomationError) as exc:
            return await self._fail(project.id, exc, actor_user_id=actor_user_id)
        state = (result or {}).get("state")
        if state != _ACTIVE:
            # Keep the id so a retry updates this watcher instead of making another.
            return await asyncio.to_thread(
                self._store.set_state,
                project.id,
                "error",
                actor_user_id=actor_user_id,
                last_error=f"The watcher was saved but is {state or 'not running'}; retry",
                automation_id=task_id,
                relay_key_hash=relay_key_hash(relay_key),
                action="provision_error",
            )
        return await asyncio.to_thread(
            self._store.set_state,
            project.id,
            "active",
            actor_user_id=actor_user_id,
            automation_id=task_id,
            relay_key_hash=relay_key_hash(relay_key),
            action="provisioned",
        )

    async def pause(
        self, project: Project, binding: SlackBinding, *, actor_user_id: str | None
    ) -> SlackBinding:
        """Pause the watcher; the link becomes ``disabled`` and keeps its channel."""
        if binding.automation_id:
            try:
                token = await self._credential(project, binding)
                await self._client(binding).set_state(
                    token, _task_id(binding.automation_id), _PAUSED
                )
            except (AutomationAuthError, AutomationError) as exc:
                # Disabled here regardless: the relay refuses a disabled link's calls.
                return await self._fail(
                    project.id, exc, actor_user_id=actor_user_id, keep="disabled"
                )
        return await asyncio.to_thread(
            self._store.set_state, project.id, "disabled", actor_user_id=actor_user_id
        )

    async def remove(
        self, project: Project, binding: SlackBinding, *, actor_user_id: str | None
    ) -> None:
        """Delete the watcher before the link is released.

        A watcher that can't be deleted is recorded; it can do nothing once the
        link is gone, because the relay refuses calls for unknown links.
        """
        if not binding.automation_id:
            return
        try:
            token = await self._credential(project, binding)
            await self._client(binding).delete(token, _task_id(binding.automation_id))
        except (AutomationAuthError, AutomationError) as exc:
            await asyncio.to_thread(
                self._store.record,
                project.id,
                "automation_orphaned",
                binding_id=binding.id,
                actor_user_id=actor_user_id,
                detail={"automation_id": binding.automation_id, "error": str(exc)[:500]},
            )

    async def overlaps(self, project: Project, binding: SlackBinding) -> list[str]:
        """Other automations of the owner that also watch this channel (best effort).

        Uniqueness here can't stop another server or a hand-made automation from
        watching the same channel; this only finds the owner's own.
        """
        if not binding.automation_id:
            return []
        try:
            token = await self._credential(project, binding)
            own_id = _task_id(binding.automation_id)
            tasks = await self._client(binding).list(token)
        except (AutomationAuthError, AutomationError):
            return []
        found = []
        for task in tasks:
            if str(task.get("id")) == own_id:
                continue
            if binding.channel_id in str(task.get("prompt") or ""):
                found.append(str(task.get("name") or task.get("id")))
        return found
