"""Project settings → Slack: link a project to one Slack channel.

Any member can see the link; only a product approver (the owner or a configured
approver) can link, change, disable, enable or unlink it. Linking claims the
channel first: the database refuses a channel another project holds. The
watcher automation is then provisioned for the link (see
:mod:`omnigent.server.slack_provisioning`); disabling pauses it and keeps the
claim, and unlinking deletes it and frees the channel.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.product_approval import product_approvers
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.project_slack_store import ChannelTakenError, SlackBinding

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.server.auth import AuthProvider
    from omnigent.stores import ProjectStore
    from omnigent.stores.project_slack_store import ProjectSlackStore

TEAM_ID = re.compile(r"^[TE][A-Z0-9]{2,30}$")
CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{2,30}$")
# https://app.slack.com/client/T0123/C0456 carries both ids.
_CLIENT_LINK = re.compile(r"^https://app\.slack\.com/client/([A-Z0-9]+)/([A-Z0-9]+)/?(?:[?#].*)?$")
# https://<workspace>.slack.com/archives/C0456 carries only the channel.
_ARCHIVE_LINK = re.compile(
    r"^https://[a-z0-9-]+\.(?:enterprise\.)?slack\.com/archives/([A-Z0-9]+)/?(?:[?#].*)?$"
)
_DESTINATION_KEYS = ("org", "repo", "model", "username", "automation_url")


class SlackLinkBody(BaseModel):
    """What a product approver enters in Project settings → Slack."""

    model_config = ConfigDict(extra="forbid")

    channel_link: str | None = Field(
        default=None,
        max_length=512,
        description="A Slack channel link, e.g. https://<workspace>.slack.com/archives/<channel> "
        "(Copy link in Slack) or https://app.slack.com/client/<team>/<channel>. The "
        "workspace ID is optional; the watcher records it on its first run.",
    )
    slack_team_id: str | None = Field(default=None, max_length=32)
    channel_id: str | None = Field(default=None, max_length=32)
    channel_name: str | None = Field(default=None, max_length=128)
    destination: dict[str, str] | None = Field(
        default=None,
        description="Where the watcher automation runs: automation_url (the automation "
        "service's scheduled-task API, e.g. https://<workspace>/api/2.0/<service>), org, "
        "repo, model, and the owner's username on the automation service. "
        "Omit a setting to use the default.",
    )


def parse_channel(body: SlackLinkBody) -> tuple[str | None, str]:
    """The ``(slack_team_id, channel_id)`` a link body names; the team may be ``None``.

    :raises OmnigentError: ``INVALID_INPUT`` when the IDs are missing or malformed.
    """
    team_id = (body.slack_team_id or "").strip().upper() or None
    channel_id = (body.channel_id or "").strip().upper() or None
    link = (body.channel_link or "").strip()
    if link:
        if match := _CLIENT_LINK.match(link):
            team_id, channel_id = match.group(1), match.group(2)
        elif match := _ARCHIVE_LINK.match(link):
            channel_id = match.group(1)
        else:
            raise OmnigentError(
                "Paste a Slack channel link, e.g. https://app.slack.com/client/T0123/C0456",
                code=ErrorCode.INVALID_INPUT,
            )
    if channel_id is None or not CHANNEL_ID.match(channel_id):
        raise OmnigentError("Give a Slack channel ID like C0123ABCD", code=ErrorCode.INVALID_INPUT)
    if team_id is not None and not TEAM_ID.match(team_id):
        raise OmnigentError(
            "A Slack workspace (team) ID looks like T0123ABCD; or leave it out",
            code=ErrorCode.INVALID_INPUT,
        )
    return team_id, channel_id


def clean_automation_url(value: str) -> str:
    """The scheduled-task API base a link names, e.g. ``https://<workspace>/api/2.0/<service>``.

    :raises OmnigentError: ``INVALID_INPUT`` for anything but a plain HTTPS API URL.
    """
    parts = urlsplit(value.strip())
    path = parts.path.rstrip("/").removesuffix("/scheduled-tasks")
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not path.startswith("/api/")
    ):
        raise OmnigentError(
            "Give the automation service's API URL, like https://<workspace>/api/2.0/<service>, "
            "not the Tasks page in the browser",
            code=ErrorCode.INVALID_INPUT,
        )
    return f"https://{parts.netloc}{path}"


def clean_destination(destination: dict[str, str] | None) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in (destination or {}).items():
        if key not in _DESTINATION_KEYS:
            raise OmnigentError(
                f"Unknown destination setting {key!r}; use automation_url, org, repo, model "
                "or username",
                code=ErrorCode.INVALID_INPUT,
            )
        value = str(value).strip()
        if not value:
            continue
        if key == "automation_url":
            cleaned[key] = clean_automation_url(value[:512])
        else:
            cleaned[key] = value[:128]
    return cleaned


def request_base_url(request: Request) -> str:
    """The address the caller used to reach this server, behind a proxy too."""
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if not host:
        return str(request.base_url).rstrip("/")
    return f"{proto or request.url.scheme}://{host}"


def binding_json(binding: SlackBinding | None, *, can_manage: bool) -> dict[str, Any]:
    """The link as Project settings shows it; never any credential."""
    if binding is None:
        return {"object": "project.slack", "linked": False, "can_manage": can_manage}
    return {
        "object": "project.slack",
        "linked": True,
        "can_manage": can_manage,
        "id": binding.id,
        "slack_team_id": binding.slack_team_id,
        "channel_id": binding.channel_id,
        "channel_name": binding.channel_name,
        "state": binding.state,
        "destination": binding.destination,
        "automation_id": binding.automation_id,
        "last_error": binding.last_error,
        "owner_user_id": binding.owner_user_id,
        "updated_by_user_id": binding.updated_by_user_id,
        "updated_at": binding.updated_at,
    }


def create_project_slack_router(
    project_store: ProjectStore,
    slack_store: ProjectSlackStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/projects/{project_id}/slack")

    async def load(request: Request, project_id: str) -> tuple[str | None, Project, bool]:
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        can_manage = user_id is None or user_id in product_approvers(project)
        return user_id, project, can_manage

    async def load_manager(request: Request, project_id: str) -> tuple[str, Project]:
        """A person who may change the link; never an agent, even one running as them.

        Linking provisions a watcher with the owner's connection, so only people,
        in the UI or over the API without an agent's headers, may do it.
        """
        from omnigent.server.routes.projects import is_agent_call

        if is_agent_call(request):
            raise OmnigentError(
                "Agents can't change a project's Slack link; a person does it in Project settings",
                code=ErrorCode.FORBIDDEN,
            )
        user_id, project, can_manage = await load(request, project_id)
        if not can_manage:
            raise OmnigentError(
                "Only the project owner or a product approver can change its Slack link",
                code=ErrorCode.FORBIDDEN,
            )
        return user_id or project.user_id or "local", project

    def provisioner(request: Request) -> Any:
        return getattr(request.app.state, "slack_provisioner", None)

    @router.get("")
    async def get_project_slack(request: Request, project_id: str) -> dict[str, Any]:
        """The project's Slack channel link, its state, and whether the caller may change it."""
        _, project, can_manage = await load(request, project_id)
        binding = await asyncio.to_thread(slack_store.get, project.id)
        body = binding_json(binding, can_manage=can_manage)
        active = provisioner(request)
        if binding is not None and can_manage and active is not None:
            body["overlaps"] = await active.overlaps(project, binding)
        return body

    @router.put("")
    async def link_project_slack(
        request: Request, project_id: str, body: SlackLinkBody
    ) -> dict[str, Any]:
        """Link the project to a Slack channel, or change the link.

        The channel is claimed before anything is provisioned; a channel another
        project holds is refused with 409. The project owner owns the watcher.
        """
        actor, project = await load_manager(request, project_id)
        team_id, channel_id = parse_channel(body)
        destination = clean_destination(body.destination)
        existing = await asyncio.to_thread(slack_store.get, project.id)
        previous = existing.destination if existing is not None else {}
        if actor == (project.user_id or actor):
            # The watcher calls back on the address the owner saved the link from.
            destination["relay_url"] = request_base_url(request)
        else:
            # The owner's login signs calls to the automation URL and the owner's
            # watcher sends channel messages to the relay URL, so only they choose them.
            requested = destination.get("automation_url", previous.get("automation_url"))
            if requested != previous.get("automation_url"):
                raise OmnigentError(
                    "Only the project owner can set the automation API URL",
                    code=ErrorCode.FORBIDDEN,
                )
            for key in ("automation_url", "relay_url"):
                destination.pop(key, None)
                if previous.get(key):
                    destination[key] = previous[key]
        active = provisioner(request)
        if active is not None and existing is not None and existing.automation_id:
            moved_to = destination.get("automation_url") or active.config.api_url
            if moved_to.rstrip("/") != active.config.api_url_for(existing):
                raise OmnigentError(
                    "This link's watcher runs in another automation workspace. Unlink, then "
                    "link again to move it.",
                    code=ErrorCode.CONFLICT,
                )
        try:
            binding = await asyncio.to_thread(
                slack_store.claim,
                project.id,
                slack_team_id=team_id,
                channel_id=channel_id,
                channel_name=(body.channel_name or "").strip().lstrip("#") or None,
                destination=destination,
                owner_user_id=project.user_id or actor,
                actor_user_id=actor,
            )
        except ChannelTakenError:
            raise OmnigentError(
                "That Slack channel is already linked to another project. Unlink it there "
                "first; a channel can belong to one project.",
                code=ErrorCode.CONFLICT,
            ) from None
        if active is not None:
            binding = await active.provision(project, binding, actor_user_id=actor)
        return binding_json(binding, can_manage=True)

    @router.post("/disable")
    async def disable_project_slack(request: Request, project_id: str) -> dict[str, Any]:
        """Stop watching the channel but keep the claim; the watcher is paused."""
        actor, project = await load_manager(request, project_id)
        binding = await asyncio.to_thread(slack_store.get, project.id)
        if binding is None:
            raise OmnigentError("This project has no Slack link", code=ErrorCode.NOT_FOUND)
        active = provisioner(request)
        if active is not None:
            binding = await active.pause(project, binding, actor_user_id=actor)
        else:
            binding = await asyncio.to_thread(
                slack_store.set_state, project.id, "disabled", actor_user_id=actor
            )
        return binding_json(binding, can_manage=True)

    @router.post("/enable")
    async def enable_project_slack(request: Request, project_id: str) -> dict[str, Any]:
        """Resume watching a disabled link (or retry one that failed)."""
        actor, project = await load_manager(request, project_id)
        binding = await asyncio.to_thread(slack_store.get, project.id)
        if binding is None:
            raise OmnigentError("This project has no Slack link", code=ErrorCode.NOT_FOUND)
        active = provisioner(request)
        if active is not None:
            binding = await active.provision(project, binding, actor_user_id=actor)
        else:
            binding = await asyncio.to_thread(
                slack_store.set_state, project.id, "claimed", actor_user_id=actor, action="enabled"
            )
        return binding_json(binding, can_manage=True)

    @router.delete("")
    async def unlink_project_slack(request: Request, project_id: str) -> dict[str, Any]:
        """Unlink: delete the watcher, then free the channel for any project."""
        actor, project = await load_manager(request, project_id)
        binding = await asyncio.to_thread(slack_store.get, project.id)
        if binding is None:
            return binding_json(None, can_manage=True)
        active = provisioner(request)
        if active is not None:
            await active.remove(project, binding, actor_user_id=actor)
        await asyncio.to_thread(slack_store.release, project.id, actor_user_id=actor)
        return binding_json(None, can_manage=True)

    @router.get("/audit")
    async def project_slack_audit(request: Request, project_id: str) -> dict[str, Any]:
        """Recent link changes and relay events, newest first; product approvers only."""
        _, project = await load_manager(request, project_id)
        entries = await asyncio.to_thread(slack_store.audit, project.id)
        return {
            "object": "list",
            "data": [
                {
                    "id": entry.id,
                    "action": entry.action,
                    "actor_user_id": entry.actor_user_id,
                    "detail": entry.detail,
                    "created_at": entry.created_at,
                }
                for entry in entries
            ],
        }

    return router
