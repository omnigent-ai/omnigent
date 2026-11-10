"""List projects and file the owner's existing sessions into them via the session APIs."""

import asyncio
import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_requests import normalize_session_id
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.tools.builtins.session_projects import (
    MAX_LISTED_SESSIONS,
    MAX_PROJECT_NAME_LENGTH,
    MAX_SESSIONS_PER_MOVE,
)

_DEFAULT_LISTED_SESSIONS = 50
_SNAPSHOT_PARAMS = {"include_items": "false", "include_liveness": "false"}
_CONCURRENT_NAME_READS = 8


async def resolve_project_id(
    client: httpx.AsyncClient, name: str, *, headers: dict[str, str] | None = None
) -> tuple[str, bool]:
    """
    Return the id of the caller's project named *name* and whether it was created.

    Matches the web picker: an exact name match wins, otherwise the project is
    created, and a create that loses a race to the same name (409) reuses the
    winner's project.

    :param client: HTTP client pointed at the Omnigent server.
    :param name: The project name, e.g. ``"nightly triage"``.
    :param headers: Extra headers for the create request, e.g. the orchestration source.
    :returns: ``(project_id, created)``.
    :raises ValueError: If the name is blank or the project can't be resolved.
    """
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("project_name must not be empty")

    async def find() -> str | None:
        resp = await client.get("/v1/projects", timeout=30.0)
        if resp.status_code >= 400:
            raise ValueError(f"could not list projects: {resp.text[:200]}")
        projects = resp.json().get("data", [])
        return next((p["id"] for p in projects if p.get("name") == trimmed), None)

    existing = await find()
    if existing is not None:
        return existing, False
    created = await client.post(
        "/v1/projects", json={"name": trimmed}, headers=headers, timeout=30.0
    )
    if created.status_code < 400:
        return str(created.json()["id"]), True
    racer = await find() if created.status_code == 409 else None
    if racer is not None:
        return racer, False
    raise ValueError(f"could not create project {trimmed!r}: {created.text[:200]}")


def _same_session(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return normalize_session_id(left) == normalize_session_id(right)
    except ValueError:
        return left == right


async def _project_names(
    client: httpx.AsyncClient, project_ids: set[str], known: dict[str, str]
) -> dict[str, str]:
    """Add the names of *project_ids* missing from *known*, reading each owned project."""
    names = dict(known)
    gate = asyncio.Semaphore(_CONCURRENT_NAME_READS)

    async def read(project_id: str) -> None:
        async with gate:
            response = await client.get(f"/v1/projects/{project_id}", timeout=30.0)
        if not response.is_error:
            names[project_id] = str(response.json().get("name") or project_id)

    await asyncio.gather(*(read(project_id) for project_id in project_ids - names.keys()))
    return names


def _project_name(session: dict[str, Any], names_by_id: dict[str, str]) -> str | None:
    project_id = session.get("project_id")
    if isinstance(project_id, str) and project_id:
        return names_by_id.get(project_id, project_id)
    label = (session.get("labels") or {}).get(PROJECT_LABEL_KEY)
    return label if isinstance(label, str) and label else None


def _http_error(tool_name: str, response: httpx.Response) -> str:
    return json.dumps(
        {
            "error": f"{tool_name} failed: HTTP {response.status_code}",
            "detail": response.text[:1000],
        }
    )


async def execute_project_list(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    List a page of the caller's projects and a page of their sessions with each one's project.

    :param args: Tool arguments: optional ``project``, ``limit``, ``after`` and ``projects_after``.
    :param client: HTTP client pointed at the Omnigent server.
    :param source_id: The calling session, marked ``this_session`` in the output.
    :returns: JSON with ``projects``, ``projects_next_cursor``, ``sessions`` and ``next_cursor``.
    """
    if client is None:
        return json.dumps({"error": "sys_project_list requires server access"})
    project = args.get("project")
    if project is not None and (
        not isinstance(project, str) or len(project) > MAX_PROJECT_NAME_LENGTH
    ):
        return json.dumps({"error": "project must be a name of at most 100 characters"})
    limit = args.get("limit", _DEFAULT_LISTED_SESSIONS)
    if isinstance(limit, bool) or not isinstance(limit, int) or not limit >= 1:
        return json.dumps({"error": f"limit must be an integer from 1 to {MAX_LISTED_SESSIONS}"})
    limit = min(limit, MAX_LISTED_SESSIONS)
    cursors = {key: args.get(key) for key in ("after", "projects_after")}
    if any(value is not None and not isinstance(value, str) for value in cursors.values()):
        return json.dumps({"error": "after and projects_after must be cursor strings"})
    session_params: dict[str, str | int] = {
        "visibility": "mine",
        "kind": "default",
        "sort_by": "updated_at",
        "limit": limit,
    }
    if project is not None:
        session_params["project"] = project.strip()
    if cursors["after"]:
        session_params["after"] = cursors["after"]
    project_params: dict[str, str | int] = {"limit": limit}
    if cursors["projects_after"]:
        project_params["after"] = cursors["projects_after"]
    try:
        projects_response = await client.get("/v1/projects", params=project_params, timeout=30.0)
        if projects_response.is_error:
            return _http_error("sys_project_list", projects_response)
        sessions_response = await client.get("/v1/sessions", params=session_params, timeout=30.0)
        if sessions_response.is_error:
            return _http_error("sys_project_list", sessions_response)
        project_page = projects_response.json()
        projects = project_page.get("data", [])
        page = sessions_response.json()
        listed = page.get("data", [])
        names_by_id = await _project_names(
            client,
            {
                s["project_id"]
                for s in listed
                if isinstance(s.get("project_id"), str) and s["project_id"]
            },
            {p["id"]: p["name"] for p in projects if p.get("id")},
        )
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"sys_project_list: server unreachable: {exc}"})
    sessions = []
    for session in listed:
        family = session.get("family") or {}
        entry: dict[str, Any] = {
            "session_id": session.get("id"),
            "title": session.get("title"),
            "status": session.get("status"),
            "project": _project_name(session, names_by_id),
        }
        representative = family.get("representative_id")
        if representative and representative != session.get("id"):
            entry["moves_with"] = representative
        if _same_session(session.get("id"), source_id):
            entry["this_session"] = True
        sessions.append(entry)
    has_more = bool(page.get("has_more"))
    projects_have_more = bool(project_page.get("has_more"))
    return json.dumps(
        {
            "projects": [{"name": p["name"], "id": p.get("id")} for p in projects],
            "projects_next_cursor": project_page.get("last_id") if projects_have_more else None,
            "sessions": sessions,
            "has_more": has_more,
            "next_cursor": page.get("last_id") if has_more else None,
        }
    )


def _refusal(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else body.get("detail")
    except (ValueError, AttributeError):
        detail = None
    if not isinstance(detail, str) or not detail:
        detail = response.text[:300]
    if response.status_code == 404:
        return f"session not found or not visible to you ({detail})"
    if response.status_code == 409 and "representative" in detail:
        return "this session moves with its family; move the moves_with session instead"
    return f"HTTP {response.status_code}: {detail}"


async def _movable_snapshot(
    client: httpx.AsyncClient, session_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(snapshot, None)`` for a session this tool may move, else ``(None, error)``."""
    try:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}", params=_SNAPSHOT_PARAMS, timeout=30.0
        )
        if snapshot.is_error:
            return None, _refusal(snapshot)
        current = snapshot.json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"session could not be read: {exc}"
    level = current.get("permission_level")
    # Filing is owner-gated; refuse here so a readable foreign session is never "unchanged".
    if isinstance(level, int) and level < LEVEL_OWNER:
        return None, "only the session's owner can move it between projects"
    parent = current.get("parent_session_id")
    if parent:
        return None, f"sub-agent sessions follow their parent; move {parent} instead"
    representative = (current.get("family") or {}).get("representative_id")
    if representative and representative != current.get("id"):
        return None, f"this session moves with its family; move {representative} instead"
    return current, None


async def _move_one(
    client: httpx.AsyncClient,
    current: dict[str, Any],
    session_id: str,
    *,
    project_id: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    labels = current.get("labels") or {}
    if (current.get("project_id") or "") == project_id and not labels.get(PROJECT_LABEL_KEY):
        return {"status": "unchanged"}
    payload = {"project_id": project_id, "labels": {PROJECT_LABEL_KEY: ""}}
    try:
        moved = await client.patch(
            f"/v1/sessions/{session_id}", json=payload, headers=headers, timeout=30.0
        )
    except httpx.HTTPError as exc:
        return {"status": "error", "error": f"move could not be confirmed: {exc}"}
    if moved.is_error:
        return {"status": "error", "error": _refusal(moved)}
    return {"status": "moved"}


async def _server_checks_move_source(client: httpx.AsyncClient) -> bool:
    """Whether the server refuses moves whose calling session is shared with edit access."""
    try:
        info = await client.get("/v1/info", timeout=30.0)
        body = info.json() if info.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(body, dict) and body.get("session_move_source_checks") is True


def _unique_targets(session_ids: list[str]) -> list[tuple[str, str | None]]:
    """Pair each requested ID with its canonical form, once per session."""
    targets: dict[str, tuple[str, str | None]] = {}
    for raw_id in session_ids:
        try:
            canonical: str | None = normalize_session_id(raw_id)
        except ValueError:
            canonical = None
        targets.setdefault(canonical or raw_id, (raw_id, canonical))
    return list(targets.values())


async def execute_session_move(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    File each listed session into the named project, or unfile it for ``""``.

    Each move is the sidebar's ``PATCH /v1/sessions/{id}``, which only changes
    folder membership, so a running session keeps its turn. The calling session
    is sent as the orchestration source on every write, so the server refuses
    both the moves and any project creation from a session shared with edit
    access. A project is created only when at least one session can move.

    :param args: Tool arguments: ``session_ids`` and ``project``.
    :param client: HTTP client pointed at the Omnigent server.
    :param source_id: The calling session.
    :returns: JSON with the resolved ``project``, ``project_created`` and per-session ``results``.
    """
    session_ids = args.get("session_ids")
    project = args.get("project")
    if (
        not isinstance(session_ids, list)
        or not 1 <= len(session_ids) <= MAX_SESSIONS_PER_MOVE
        or not all(isinstance(item, str) for item in session_ids)
    ):
        return json.dumps(
            {"error": f"session_ids must list 1 to {MAX_SESSIONS_PER_MOVE} session IDs"}
        )
    if not isinstance(project, str) or len(project) > MAX_PROJECT_NAME_LENGTH:
        return json.dumps(
            {"error": 'project must be a name of at most 100 characters, or "" to unfile'}
        )
    if client is None or source_id is None:
        return json.dumps({"error": "sys_session_move requires an authenticated runner"})
    if not await _server_checks_move_source(client):
        return json.dumps(
            {
                "error": "sys_session_move: this Omnigent server cannot check the calling "
                "session's sharing before a move; upgrade the server or move the sessions "
                "from the sidebar"
            }
        )
    results: list[dict[str, Any]] = []
    movable: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for raw_id, canonical in _unique_targets(session_ids):
        result: dict[str, Any] = {"session_id": raw_id, "status": "error"}
        results.append(result)
        if canonical is None:
            result["error"] = "invalid session ID"
            continue
        current, error = await _movable_snapshot(client, canonical)
        if current is None:
            result["error"] = error
        else:
            movable.append((result, current, canonical))
    name = project.strip()
    headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
    project_id, created = "", False
    if name and movable:
        try:
            project_id, created = await resolve_project_id(client, name, headers=headers)
        except (httpx.HTTPError, ValueError) as exc:
            return json.dumps({"error": f"sys_session_move: {exc}"})
    for result, current, canonical in movable:
        result.update(
            await _move_one(client, current, canonical, project_id=project_id, headers=headers)
        )
    return json.dumps({"project": name or None, "project_created": created, "results": results})
