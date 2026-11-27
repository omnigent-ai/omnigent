"""Provision the Slack watcher automation for a project's channel link.

The watcher runs as a scheduled task on an agent automation service in another
workspace, owned by the project owner. This server creates, updates, pauses and
deletes it with the owner's Databricks connection to that workspace, so people
manage everything from Project settings and never edit the automation.

Each provisioning mints a fresh per-link relay key, stores only its hash, and
writes the key into the automation's instructions; the watcher presents it on
every relay call. Configured by environment (see :meth:`SlackAutomationConfig.from_env`);
with no automation URL set, links stay ``claimed`` and nothing is provisioned.
"""

from __future__ import annotations

import asyncio
import hashlib
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


def relay_key_hash(relay_key: str) -> str:
    return hashlib.sha256(relay_key.encode()).hexdigest()


def _host(url: str) -> str:
    return (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()


@dataclass(frozen=True)
class SlackAutomationConfig:
    """Where watcher automations run and how they call back.

    :param api_url: Base URL of the automation service's scheduled-task API, e.g.
        ``https://automation.example.com/api/2.0/agent``.
    :param relay_url: This server's public URL, which the watcher calls.
    :param org: Default repository org for the automation's sandbox.
    :param repo: Default repository name.
    :param branch: Default base branch.
    :param model: Default model, or ``""`` for the service's default.
    :param cron: How often the watcher polls the channel.
    """

    api_url: str
    relay_url: str
    org: str
    repo: str
    branch: str = "main"
    model: str = ""
    cron: str = "*/2 * * * *"

    @property
    def workspace_host(self) -> str:
        return _host(self.api_url)

    @classmethod
    def from_env(cls) -> SlackAutomationConfig | None:
        api_url = os.environ.get("OMNIGENT_SLACK_AUTOMATION_API_URL", "").strip().rstrip("/")
        if not api_url:
            return None
        relay_url = os.environ.get("OMNIGENT_SLACK_RELAY_URL", "").strip().rstrip("/")
        if not relay_url:
            domain = os.environ.get("OMNIGENT_DOMAIN", "").strip()
            relay_url = f"https://{domain}" if domain else ""
        org = os.environ.get("OMNIGENT_SLACK_AUTOMATION_ORG", "").strip()
        repo = os.environ.get("OMNIGENT_SLACK_AUTOMATION_REPO", "").strip()
        if not relay_url or not org or not repo:
            _logger.warning(
                "OMNIGENT_SLACK_AUTOMATION_API_URL is set but the relay URL, org or repo "
                "is missing; Slack channel watchers stay off."
            )
            return None
        return cls(
            api_url=api_url,
            relay_url=relay_url,
            org=org,
            repo=repo,
            branch=os.environ.get("OMNIGENT_SLACK_AUTOMATION_BRANCH", "").strip() or "main",
            model=os.environ.get("OMNIGENT_SLACK_AUTOMATION_MODEL", "").strip(),
            cron=os.environ.get("OMNIGENT_SLACK_AUTOMATION_CRON", "").strip() or "*/2 * * * *",
        )


class AutomationAuthError(Exception):
    """The owner's connection was refused; they need to reconnect."""


class AutomationError(Exception):
    """The automation service refused or failed a request."""


class AutomationClient:
    """Scheduled-task calls on the automation service, as the bearer's owner.

    Only these five calls are made; nothing else is reachable through it.
    """

    def __init__(self, api_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = f"{api_url.rstrip('/')}/scheduled-tasks"
        self._transport = transport

    async def _call(
        self, method: str, path: str, token: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, transport=self._transport) as http:
            try:
                response = await http.request(
                    method,
                    f"{self._base}{path}",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise AutomationError(f"The automation service is unreachable: {exc}") from exc
        if response.status_code in (401, 403):
            raise AutomationAuthError(
                f"The automation service refused the owner's connection ({response.status_code})"
            )
        if response.status_code == 404 and method == "DELETE":
            return {}
        if response.status_code >= 400:
            raise AutomationError(
                f"The automation service returned {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else {}

    async def create(self, token: str, task: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "", token, task)

    async def update(
        self, token: str, owner: str, task_id: str, task: dict[str, Any]
    ) -> dict[str, Any]:
        body = {
            "username": owner,
            "schedule_id": task_id,
            "display_name": task["display_name"],
            "spec": task["spec"],
            "trigger": task["trigger"],
        }
        return await self._call("PATCH", f"/{owner}/{task_id}", token, body)

    async def set_state(self, token: str, owner: str, task_id: str, state: str) -> dict[str, Any]:
        body = {"username": owner, "schedule_id": task_id, "state": state}
        return await self._call("POST", f"/{owner}/{task_id}/state", token, body)

    async def delete(self, token: str, owner: str, task_id: str) -> None:
        await self._call("DELETE", f"/{owner}/{task_id}", token)

    async def list(self, token: str, owner: str) -> list[dict[str, Any]]:
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
    ) -> None:
        self.config = config
        self._store = store
        self._connections = connection_store
        self._connection_client = connection_client
        self._client = client or AutomationClient(config.api_url)

    async def _owner_token(self, owner_user_id: str) -> str:
        """The owner's access token for the automation workspace.

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
                f"The project owner needs to connect Databricks to {self.config.workspace_host} "
                "(Settings → Connections), then retry"
            )
        token, workspace_host = resolved
        if _host(workspace_host) != self.config.workspace_host:
            raise AutomationAuthError(
                f"The project owner's Databricks connection is for {_host(workspace_host)}, "
                f"not {self.config.workspace_host}; reconnect it there, then retry"
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
                relay_url=self.config.relay_url,
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
        relay_key = secrets.token_urlsafe(32)
        try:
            token = await self._owner_token(binding.owner_user_id)
            task = self._task(project, binding, relay_key)
            if binding.automation_id:
                owner, task_id = _split_automation_id(binding.automation_id)
                await self._client.update(token, owner, task_id, task)
                await self._client.set_state(token, owner, task_id, _ACTIVE)
                automation_id = binding.automation_id
            else:
                created = await self._client.create(token, {"scheduled_task": task})
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
                token = await self._owner_token(binding.owner_user_id)
                owner, task_id = _split_automation_id(binding.automation_id)
                await self._client.set_state(token, owner, task_id, _PAUSED)
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
            token = await self._owner_token(binding.owner_user_id)
            owner, task_id = _split_automation_id(binding.automation_id)
            await self._client.delete(token, owner, task_id)
        except (AutomationAuthError, AutomationError) as exc:
            await asyncio.to_thread(
                self._store.record,
                project.id,
                "automation_orphaned",
                binding_id=binding.id,
                actor_user_id=actor_user_id,
                detail={"automation_id": binding.automation_id, "error": str(exc)[:500]},
            )

    async def overlaps(self, binding: SlackBinding) -> list[str]:
        """Other automations of the owner that also watch this channel (best effort).

        Uniqueness here can't stop another server or a hand-made automation from
        watching the same channel; this only finds the owner's own.
        """
        if not binding.automation_id:
            return []
        try:
            token = await self._owner_token(binding.owner_user_id)
            owner, own_id = _split_automation_id(binding.automation_id)
            tasks = await self._client.list(token, owner)
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
