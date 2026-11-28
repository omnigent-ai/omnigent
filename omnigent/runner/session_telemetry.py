"""Session-level span attributes the runner hands each harness turn."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Mapping, MutableMapping
from typing import Any

import httpx

from omnigent.diagnostics import redact_url
from omnigent.inner.tracing import (
    AGENT_MODEL,
    SESSION_MODEL,
    SESSION_MODEL_SOURCE,
    SESSION_REASONING_EFFORT,
    SOURCE_OBSERVED,
    SOURCE_REQUESTED,
    SOURCE_RESOLVED_DEFAULT,
)
from omnigent.runner.github_resource import _git
from omnigent.runner.native import _SESSION_METADATA_PARAMS
from omnigent.runtime.telemetry import should_capture_content

_logger = logging.getLogger(__name__)

# Metadata reads must never hold a turn: each server GET is capped, and the
# whole resolution shares one budget before the turn goes on without it.
_SERVER_TIMEOUT_S = 5.0
_RESOLVE_TIMEOUT_S = 5.0

LABEL_ATTRIBUTE_PREFIX = "omnigent.session.labels."


def session_model(
    session: Mapping[str, Any], configured_model: str | None
) -> tuple[str, str] | None:
    """
    Resolve the model a session is running and where that value came from.

    The snapshot's ``llm_model`` is the harness's reported model, else the
    agent's configured one, so a value other than *configured_model* is a
    harness report. Without a report, a session model override is what the
    harness was asked to run, else the configured model is the default.

    :param session: Session response body, e.g. ``{"llm_model": "gpt-5.5",
        "model_override": None}``.
    :param configured_model: The session agent's configured model, or ``None``.
    :returns: ``(model, source)``, e.g. ``("claude-opus-4-8[1m]", "observed")``,
        or ``None`` when no model is known.
    """
    llm_model = session.get("llm_model")
    override = session.get("model_override")
    if isinstance(llm_model, str) and llm_model and llm_model != configured_model:
        return llm_model, SOURCE_OBSERVED
    if isinstance(override, str) and override:
        return override, SOURCE_REQUESTED
    if isinstance(llm_model, str) and llm_model:
        return llm_model, SOURCE_RESOLVED_DEFAULT
    if configured_model:
        return configured_model, SOURCE_RESOLVED_DEFAULT
    return None


def session_metadata_attributes(
    session: Mapping[str, Any],
    *,
    project_name: str | None = None,
    git_branch: str | None = None,
    configured_model: str | None = None,
) -> dict[str, str]:
    """
    Project a ``GET /v1/sessions/{id}`` body onto ``omnigent.*`` span attributes.

    Missing or empty values are omitted, as are internal ``omnigent.*`` labels.
    The title is often generated from the first message, so it is included
    only when ``OMNIGENT_OTEL_CAPTURE_CONTENT`` is on.

    :param session: Session response body, e.g. ``{"title": "Fix login",
        "labels": {"team": "web"}, "project_id": "proj_1"}``.
    :param project_name: Name of the session's project, if resolved.
    :param git_branch: Branch to report when the session records none.
    :param configured_model: The session agent's configured model, reported
        as such and used to tell a harness-reported model from the default
        (:func:`session_model`).
    :returns: Attribute names mapped to values, e.g.
        ``{"omnigent.session.title": "Fix login"}``.
    """
    model = session_model(session, configured_model)
    candidates: dict[str, object] = {
        "omnigent.session.title": session.get("title") if should_capture_content() else None,
        "omnigent.project.id": session.get("project_id"),
        "omnigent.project.name": project_name,
        "omnigent.git.branch": session.get("git_branch") or git_branch,
        "omnigent.agent.name": session.get("agent_name"),
        AGENT_MODEL: configured_model,
        SESSION_MODEL: model[0] if model else None,
        SESSION_MODEL_SOURCE: model[1] if model else None,
        SESSION_REASONING_EFFORT: session.get("reasoning_effort"),
    }
    labels = session.get("labels")
    if isinstance(labels, Mapping):
        candidates.update(
            {
                f"{LABEL_ATTRIBUTE_PREFIX}{key}": value
                for key, value in labels.items()
                if not str(key).startswith("omnigent.")
            }
        )
    return _string_attributes(candidates)


def local_session_attributes(workspace: str | None) -> dict[str, str]:
    """
    Resolve the attributes that depend only on this machine; never raises.

    The host name is the machine's own: the runner has no view of the host's
    configured identity name.

    :param workspace: The session's working folder, or ``None``.
    :returns: Workspace, git remote and host attributes that resolved.
    """
    try:
        host_name: str | None = socket.gethostname()
    except OSError:
        host_name = None
    return _string_attributes(
        {
            "omnigent.workspace": workspace,
            "omnigent.git.remote": git_remote_url(workspace) if workspace else None,
            "omnigent.host.name": host_name,
        }
    )


def _string_attributes(candidates: Mapping[str, object]) -> dict[str, str]:
    """Drop missing or empty values and stringify the rest."""
    return {key: str(value) for key, value in candidates.items() if value not in (None, "")}


def _git_output(argv: list[str], workspace: str) -> str | None:
    """Return stripped stdout of ``git <argv>`` in *workspace*, or ``None`` on any failure."""
    returncode, stdout, _ = _git(argv, cwd=workspace)
    output = stdout.strip()
    return output if returncode == 0 and output else None


def git_remote_url(workspace: str) -> str | None:
    """
    Return the ``origin`` URL of the git repo containing *workspace*, or ``None``.

    Credentials embedded in the URL are dropped, so ``https://user:token@host/acme/app.git``
    and ``git@host:acme/app.git`` are reported as ``https://host/acme/app.git``
    and ``host:acme/app.git``.

    :param workspace: Directory inside the repository.
    :returns: The remote URL, e.g. ``"https://github.com/acme/app.git"``.
    """
    return redact_url(_git_output(["remote", "get-url", "origin"], workspace))


def git_branch(workspace: str) -> str | None:
    """
    Return the branch checked out in *workspace*, or ``None`` (including detached HEAD).

    :param workspace: Directory inside the repository.
    :returns: The branch name, e.g. ``"main"``.
    """
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
    return branch if branch != "HEAD" else None


async def _get_json(server_client: httpx.AsyncClient, path: str, **kwargs: Any) -> dict[str, Any]:
    """GET *path* and return its JSON object body, or ``{}`` on any failure."""
    try:
        response = await server_client.get(path, timeout=_SERVER_TIMEOUT_S, **kwargs)
        if response.status_code == 200:
            body = response.json()
            if isinstance(body, dict):
                return body
    except Exception:
        _logger.debug("session telemetry lookup %s failed", path, exc_info=True)
    return {}


async def resolve_session_attributes(
    server_client: httpx.AsyncClient,
    session_id: str,
    *,
    workspace: str | None,
    local: Mapping[str, str],
    project_names: MutableMapping[str, str] | None = None,
    configured_model: str | None = None,
) -> dict[str, str]:
    """
    Resolve the current span attributes for *session_id*; never raises.

    Server-owned metadata is read on every call so renames, label edits,
    project moves and model or effort changes made mid-session reach later
    turns. When the reads do not finish within the budget, the turn goes on
    with *local* alone.

    :param server_client: Runner-to-server client.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param workspace: The session's runtime working folder, or ``None``.
    :param local: The session's cached :func:`local_session_attributes`.
    :param project_names: Project names by project id, filled on first use so
        a project is looked up once per runner process.
    :param configured_model: The session agent's configured model, or ``None``.
    :returns: Attribute names mapped to values.
    """
    try:
        metadata = await asyncio.wait_for(
            _server_session_attributes(
                server_client,
                session_id,
                workspace=workspace,
                project_names=project_names,
                configured_model=configured_model,
            ),
            timeout=_RESOLVE_TIMEOUT_S,
        )
        return {**local, **metadata}
    except TimeoutError:
        _logger.debug("session telemetry attributes for %s timed out; skipped", session_id)
    except Exception:
        _logger.debug("session telemetry attributes failed for %s", session_id, exc_info=True)
    return dict(local)


async def _server_session_attributes(
    server_client: httpx.AsyncClient,
    session_id: str,
    *,
    workspace: str | None,
    project_names: MutableMapping[str, str] | None,
    configured_model: str | None,
) -> dict[str, str]:
    """Read the session from the server and project it onto span attributes."""
    session = await _get_json(
        server_client, f"/v1/sessions/{session_id}", params=_SESSION_METADATA_PARAMS
    )
    project_name = None
    if project_id := session.get("project_id"):
        cache = project_names if project_names is not None else {}
        project_name = cache.get(project_id)
        if project_name is None:
            project = await _get_json(server_client, f"/v1/projects/{project_id}")
            if isinstance(name := project.get("name"), str) and name:
                project_name = cache[project_id] = name
    branch = None
    if not session.get("git_branch") and workspace:
        branch = await asyncio.to_thread(git_branch, workspace)
    return session_metadata_attributes(
        session,
        project_name=project_name,
        git_branch=branch,
        configured_model=configured_model,
    )
