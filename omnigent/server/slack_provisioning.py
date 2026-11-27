"""Provision the Slack watcher automation for a project's channel link.

The watcher runs as a scheduled task on an agent automation service in another
workspace, owned by the project owner. This server creates, updates, pauses and
deletes it, so people manage everything from Project settings and never edit the
automation. The calls go through the project Lead's host, signed there with the
owner's own Databricks login (see :mod:`omnigent.host.automation_requests`), so
the server holds no credential for that workspace. When the Lead's host isn't
connected, the owner's server-side Databricks connection is used if it is for
that workspace.

Each provisioning mints a fresh per-link relay key, stores only its hash, and
writes the key into the automation's instructions; the watcher presents it on
every relay call. Each link names its automation API URL in Project settings;
the relay URL is the address people use to save the link, and the repository
defaults to ``omnigent-ai/omnigent``, so no server configuration is needed. The
environment can still set defaults (see :meth:`SlackAutomationConfig.from_env`).
A link with no automation URL stays ``claimed`` and nothing is provisioned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
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
_ACTIVE = "SCHEDULED_TASK_STATE_ACTIVE"
_PAUSED = "SCHEDULED_TASK_STATE_PAUSED"
# Marks automations this server manages, so overlap checks can tell them apart.
AUTOMATION_NAME_PREFIX = "[omnigent-slack]"
# The repository the watcher's sandbox checks out unless a link names another.
DEFAULT_ORG = "omnigent-ai"
DEFAULT_REPO = "omnigent"


def relay_key_hash(relay_key: str) -> str:
    return hashlib.sha256(relay_key.encode()).hexdigest()


def _host(url: str) -> str:
    return (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()


@dataclass(frozen=True)
class SlackAutomationConfig:
    """Defaults for where watcher automations run and how they call back.

    A link's own ``automation_url`` and ``relay_url`` take precedence.

    :param api_url: Base URL of the automation service's scheduled-task API, e.g.
        ``https://automation.example.com/api/2.0/agent``, or ``""`` for none.
    :param relay_url: This server's public URL, which the watcher calls, or ``""``
        to use the address the link was saved from.
    :param org: Default repository org for the automation's sandbox.
    :param repo: Default repository name.
    :param branch: Default base branch.
    :param model: Default model, or ``""`` for the service's default.
    :param cron: How often the watcher polls the channel.
    """

    api_url: str = ""
    relay_url: str = ""
    org: str = DEFAULT_ORG
    repo: str = DEFAULT_REPO
    branch: str = "main"
    model: str = ""
    cron: str = "*/2 * * * *"

    def api_url_for(self, binding: SlackBinding) -> str:
        return ((binding.destination or {}).get("automation_url") or self.api_url).rstrip("/")

    def relay_url_for(self, binding: SlackBinding) -> str:
        return (self.relay_url or (binding.destination or {}).get("relay_url") or "").rstrip("/")

    @classmethod
    def from_env(cls) -> SlackAutomationConfig:
        """Defaults from the environment; every setting is optional."""
        relay_url = os.environ.get("OMNIGENT_SLACK_RELAY_URL", "").strip().rstrip("/")
        if not relay_url:
            domain = os.environ.get("OMNIGENT_DOMAIN", "").strip()
            relay_url = f"https://{domain}" if domain else ""
        return cls(
            api_url=os.environ.get("OMNIGENT_SLACK_AUTOMATION_API_URL", "").strip().rstrip("/"),
            relay_url=relay_url,
            org=os.environ.get("OMNIGENT_SLACK_AUTOMATION_ORG", "").strip() or DEFAULT_ORG,
            repo=os.environ.get("OMNIGENT_SLACK_AUTOMATION_REPO", "").strip() or DEFAULT_REPO,
            branch=os.environ.get("OMNIGENT_SLACK_AUTOMATION_BRANCH", "").strip() or "main",
            model=os.environ.get("OMNIGENT_SLACK_AUTOMATION_MODEL", "").strip(),
            cron=os.environ.get("OMNIGENT_SLACK_AUTOMATION_CRON", "").strip() or "*/2 * * * *",
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


class AutomationClient:
    """Scheduled-task calls on the automation service, as the bearer's owner.

    Only these five calls are made; nothing else is reachable through it.
    """

    def __init__(self, api_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = f"{api_url.rstrip('/')}/scheduled-tasks"
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
                f"The automation service refused the owner's login ({status}); sign in again"
            )
        if status == 404 and method == "DELETE":
            return {}
        if status >= 400:
            raise AutomationError(f"The automation service returned {status}: {text[:300]}")
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise AutomationError("The automation service returned a non-JSON answer") from exc

    async def create(self, token: str | ViaHost, task: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "", token, task)

    async def update(
        self, token: str | ViaHost, owner: str, task_id: str, task: dict[str, Any]
    ) -> dict[str, Any]:
        body = {
            "username": owner,
            "schedule_id": task_id,
            "display_name": task["display_name"],
            "spec": task["spec"],
            "trigger": task["trigger"],
        }
        return await self._call("PATCH", f"/{owner}/{task_id}", token, body)

    async def set_state(
        self, token: str | ViaHost, owner: str, task_id: str, state: str
    ) -> dict[str, Any]:
        body = {"username": owner, "schedule_id": task_id, "state": state}
        return await self._call("POST", f"/{owner}/{task_id}/state", token, body)

    async def delete(self, token: str | ViaHost, owner: str, task_id: str) -> None:
        await self._call("DELETE", f"/{owner}/{task_id}", token)

    async def list(self, token: str | ViaHost, owner: str) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(20):
            suffix = f"?page_size=50&page_token={page_token}" if page_token else "?page_size=50"
            page = await self._call("GET", f"/{owner}{suffix}", token)
            tasks.extend(page.get("scheduled_tasks") or [])
            page_token = page.get("next_page_token") or ""
            if not page_token:
                break
        return tasks


def automation_username(binding: SlackBinding) -> str:
    """The owner's account name on the automation service.

    Set per link as ``destination.username``; otherwise the owner's email local
    part with dots as underscores, e.g. ``ann.lee@example.com`` → ``ann_lee``.
    """
    configured = (binding.destination or {}).get("username", "").strip()
    if configured:
        return configured
    return binding.owner_user_id.split("@", 1)[0].replace(".", "_")


def _split_automation_id(automation_id: str) -> tuple[str, str]:
    owner, _, task_id = automation_id.partition("/")
    return owner, task_id


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
        destination = {
            "org": self.config.org,
            "repo": self.config.repo,
            "model": self.config.model,
            **binding.destination,
        }
        spec: dict[str, Any] = {
            "prompt": watcher_prompt(
                project_name=project.name,
                binding=binding,
                relay_url=self.config.relay_url_for(binding),
                relay_key=relay_key,
            ),
            "org": destination["org"],
            "repo": destination["repo"],
            "base_branch": self.config.branch,
            "slack_channel_id": binding.channel_id,
            "suppress_success_notifications": True,
        }
        if destination.get("model"):
            spec["model"] = destination["model"]
        label = f"#{binding.channel_name}" if binding.channel_name else binding.channel_id
        return {
            "username": automation_username(binding),
            "display_name": f"{AUTOMATION_NAME_PREFIX} {project.name} ← {label}",
            "idempotency_key": binding.id,
            "spec": spec,
            "trigger": {"cron_expression": self.config.cron},
        }

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
        missing = [
            name
            for name, value in (
                ("automation API URL", self.config.api_url_for(binding)),
                ("relay URL", self.config.relay_url_for(binding)),
            )
            if not value
        ]
        if missing:
            return await asyncio.to_thread(
                self._store.set_state,
                project.id,
                "claimed",
                actor_user_id=actor_user_id,
                last_error=f"Add the {' and '.join(missing)} in Slack settings, then save",
                action="provision_waiting",
            )
        relay_key = secrets.token_urlsafe(32)
        client = self._client(binding)
        try:
            token = await self._credential(project, binding)
            task = self._task(project, binding, relay_key)
            if binding.automation_id:
                owner, task_id = _split_automation_id(binding.automation_id)
                await client.update(token, owner, task_id, task)
                await client.set_state(token, owner, task_id, _ACTIVE)
                automation_id = binding.automation_id
            else:
                created = await client.create(token, {"scheduled_task": task})
                task_id = str(created.get("schedule_id") or "")
                owner = str(created.get("username") or task["username"])
                if not task_id or not owner:
                    raise AutomationError("The automation service didn't return the new task's id")
                automation_id = f"{owner}/{task_id}"
        except (AutomationAuthError, AutomationError) as exc:
            return await self._fail(project.id, exc, actor_user_id=actor_user_id)
        return await asyncio.to_thread(
            self._store.set_state,
            project.id,
            "active",
            actor_user_id=actor_user_id,
            automation_id=automation_id,
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
                owner, task_id = _split_automation_id(binding.automation_id)
                await self._client(binding).set_state(token, owner, task_id, _PAUSED)
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
            owner, task_id = _split_automation_id(binding.automation_id)
            await self._client(binding).delete(token, owner, task_id)
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
            owner, own_id = _split_automation_id(binding.automation_id)
            tasks = await self._client(binding).list(token, owner)
        except (AutomationAuthError, AutomationError):
            return []
        found = []
        for task in tasks:
            if (
                str(task.get("schedule_id")) == own_id
                or task.get("state") == "SCHEDULED_TASK_STATE_DELETED"
            ):
                continue
            spec = task.get("spec") or {}
            if spec.get("slack_channel_id") == binding.channel_id or binding.channel_id in str(
                spec.get("prompt") or ""
            ):
                found.append(str(task.get("display_name") or task.get("schedule_id")))
        return found
