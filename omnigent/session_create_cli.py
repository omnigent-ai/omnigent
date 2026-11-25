"""Create a session from a script: ``omnigent session create``.

The command creates the session on a connected host, queues its first message
and exits, so a plain script can start and replace workers without an agent
session calling ``sys_session_create`` in a loop. It acts as the signed-in
user, exactly like starting a session in the web UI: the server's ownership,
host, workspace and permission-mode checks apply unchanged.

The first message goes through ``POST /v1/sessions/{id}/events`` like a web
composer send, so a native terminal that is still starting receives it as a
durable startup input rather than losing it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import click
import httpx

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import (
    NATIVE_CODING_AGENTS,
    native_coding_agent_for_harness,
)
from omnigent.util.json_types import JsonObject

#: The server launches the runner on the host before it answers a host-bound
#: create, which can include a worktree and a model catalog read. This matches
#: the Python SDK's host-launch read budget.
_CREATE_TIMEOUT = httpx.Timeout(30.0, read=3660.0)
#: A first message can wait on an approval policy before the server answers.
_MESSAGE_TIMEOUT = httpx.Timeout(30.0, read=3660.0)
_READ_TIMEOUT = httpx.Timeout(30.0)


@dataclass(frozen=True)
class ScriptedSession:
    """
    What ``omnigent session create`` asks the server for.

    :param agent_id: Agent to bind, or ``None`` to take the project's saved agent.
    :param host_id: Connected host to run on, e.g. ``"host_abc123"``.
    :param workspace: Absolute directory on *host_id*, or ``None`` to take the
        project's saved workspace.
    :param title: Session title, e.g. ``"Fix flaky test"``.
    :param project_id: Project to file the session in, e.g. ``"proj_123"``.
    :param model: Model override, e.g. ``"claude-sonnet-4-6"``.
    :param reasoning_effort: Reasoning effort, e.g. ``"high"``.
    :param permission_mode: Permission mode in the harness's vocabulary, e.g.
        ``"auto"`` for Claude.
    """

    agent_id: str | None
    host_id: str
    workspace: str | None
    title: str | None = None
    project_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None


def resolve_agent_id(
    *, agent: str | None, agent_id: str | None, project_id: str | None
) -> str | None:
    """
    Turn ``--agent`` / ``--agent-id`` into the agent id to bind.

    :param agent: A native agent name as ``omnigent <agent>`` takes it, e.g.
        ``"claude"`` or ``"codex"``; a harness spelling such as
        ``"claude-native"`` also works.
    :param agent_id: A registered agent's id.
    :param project_id: The project to file in; it may supply a saved agent.
    :returns: The agent id, or ``None`` when the project's saved agent applies.
    :raises click.UsageError: When both or neither are given without a project,
        or *agent* names no native agent.
    """
    if agent is not None and agent_id is not None:
        raise click.UsageError("Pass --agent or --agent-id, not both.")
    if agent_id is not None:
        return agent_id
    if agent is None:
        if project_id is None:
            raise click.UsageError("Pass --agent (e.g. claude) or --agent-id.")
        return None
    by_key = {native.key: native for native in NATIVE_CODING_AGENTS}
    native = by_key.get(agent) or native_coding_agent_for_harness(agent)
    if native is None:
        names = ", ".join(sorted(by_key))
        raise click.UsageError(
            f"Unknown agent {agent!r}. Use one of {names}, or --agent-id for a registered agent."
        )
    return builtin_agent_id(native.agent_name)


def create_body(spec: ScriptedSession) -> JsonObject:
    """
    Build the ``POST /v1/sessions`` JSON body for *spec*.

    A field left ``None`` is omitted, so a project's saved defaults fill it.

    :param spec: The requested session.
    :returns: The request body.
    """
    body: JsonObject = {"host_type": "external", "host_id": spec.host_id}
    optional_fields: dict[str, str | None] = {
        "agent_id": spec.agent_id,
        "workspace": spec.workspace,
        "title": spec.title,
        "project_id": spec.project_id,
        "model_override": spec.model,
        "reasoning_effort": spec.reasoning_effort,
        "permission_mode": spec.permission_mode,
    }
    body.update({key: value for key, value in optional_fields.items() if value is not None})
    return body


def project_has_saved_workspace(client: httpx.Client, project_id: str) -> bool:
    """
    Report whether the project saves a workspace for its new sessions.

    :param client: Authenticated client for the server.
    :param project_id: The project, e.g. ``"proj_123"``.
    :returns: ``True`` when the project's config names a workspace.
    :raises click.ClickException: When the project cannot be read.
    """
    resp = client.get(f"/v1/projects/{project_id}", timeout=_READ_TIMEOUT)
    if resp.status_code == 404:
        raise click.ClickException(f"Project {project_id} was not found.")
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Could not read project {project_id} ({resp.status_code}): {server_error(resp)}"
        )
    config = _json_object(resp).get("config")
    return isinstance(config, dict) and isinstance(config.get("workspace"), str)


def wait_for_local_host(base_url: str, headers: dict[str, str], host_id: str) -> None:
    """
    Wait until this machine's host daemon is connected to the server.

    The daemon registers asynchronously after it starts, and a create bound to
    a host the server does not know yet is refused.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param headers: Auth and routing headers for the server.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``.
    :raises click.ClickException: When the host is not online in time.
    """
    from omnigent.host.daemon_launch import open_daemon_client, wait_for_host_online
    from omnigent.native.native_terminal import DAEMON_HOST_ONLINE_TIMEOUT_S

    async def _wait() -> None:
        async with open_daemon_client(base_url, headers, host_id, timeout=_READ_TIMEOUT) as client:
            await wait_for_host_online(client, host_id, timeout_s=DAEMON_HOST_ONLINE_TIMEOUT_S)

    asyncio.run(_wait())


def create_session(client: httpx.Client, spec: ScriptedSession) -> str:
    """
    Create the session and return its id.

    :param client: Authenticated client for the server.
    :param spec: The requested session.
    :returns: The new session id.
    :raises click.ClickException: When the server refuses the create or does not
        answer; a timed-out create may still have made a session.
    """
    try:
        resp = client.post("/v1/sessions", json=create_body(spec), timeout=_CREATE_TIMEOUT)
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Session creation could not be confirmed ({exc}). Check your sessions "
            "before creating another."
        ) from exc
    if resp.status_code >= 400:
        raise click.ClickException(
            f"The server refused the session ({resp.status_code}): {server_error(resp)}"
        )
    payload = _json_object(resp)
    session_id = payload.get("id") or payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise click.ClickException("The server created no session id.")
    return session_id


def queue_message(client: httpx.Client, session_id: str, text: str) -> None:
    """
    Queue *text* as the session's next user message.

    :param client: Authenticated client for the server.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param text: The message.
    :raises click.ClickException: When the server refused, denied or did not
        confirm the message.
    """
    event = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    try:
        resp = client.post(
            f"/v1/sessions/{session_id}/events", json=event, timeout=_MESSAGE_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Session {session_id} was created, but its message could not be confirmed "
            f"({exc}). Check the session before sending it again."
        ) from exc
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Session {session_id} was created, but the server refused its message "
            f"({resp.status_code}): {server_error(resp)}"
        )
    ack = _json_object(resp)
    if ack.get("denied") is True:
        raise click.ClickException(
            f"Session {session_id} was created, but a policy denied its message."
        )
    if ack.get("queued") is not True:
        raise click.ClickException(
            f"Session {session_id} was created, but the server did not queue its message."
        )


def server_error(resp: httpx.Response) -> str:
    """
    Return the server's error message from *resp*, or its leading text.

    :param resp: An error response, e.g. ``{"error": {"message": "..."}}``.
    :returns: The message, e.g. ``"workspace must be an absolute path"``.
    """
    error = _json_object(resp).get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(error, str):
        return error
    return resp.text[:500]


def _json_object(resp: httpx.Response) -> JsonObject:
    """
    Decode *resp* as a JSON object.

    :param resp: The HTTP response.
    :returns: The object, or ``{}`` when the body is not one.
    """
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
