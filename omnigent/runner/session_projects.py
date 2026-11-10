"""List projects and file the owner's existing sessions into them via the session APIs."""

import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_requests import normalize_session_id
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.tools.builtins.session_projects import (
    MAX_LISTED_SESSIONS,
    MAX_PROJECT_NAME_LENGTH,
    MAX_SESSIONS_PER_MOVE,
)

_DEFAULT_LISTED_SESSIONS = 50
_SNAPSHOT_PARAMS = {"include_items": "false", "include_liveness": "false"}


async def resolve_project_id(client: httpx.AsyncClient, name: str) -> tuple[str, bool]:
    """
    Return the id of the caller's project named *name* and whether it was created.

    Matches the web picker: an exact name match wins, otherwise the project is
    created, and a create that loses a race to the same name (409) reuses the
    winner's project.

    :param client: HTTP client pointed at the Omnigent server.
    :param name: The project name, e.g. ``"nightly triage"``.
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
    created = await client.post("/v1/projects", json={"name": trimmed}, timeout=30.0)
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


def _project_name(session: dict[str, Any], names_by_id: dict[str, str]) -> str | None:
    project_id = session.get("project_id")
    if isinstance(project_id, str) and project_id:
        return names_by_id.get(project_id, project_id)
    label = (session.get("labels") or {}).get(PROJECT_LABEL_KEY)
    return label if isinstance(label, str) and label else None


async def execute_project_list(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    List the caller's projects and their own sessions with each one's project.

    :param args: Tool arguments: optional ``project``, ``limit`` and ``after``.
    :param client: HTTP client pointed at the Omnigent server.
    :param source_id: The calling session, marked ``this_session`` in the output.
    :returns: JSON with ``projects``, ``sessions``, ``has_more`` and ``next_cursor``.
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
    after = args.get("after")
    if after is not None and not isinstance(after, str):
        return json.dumps({"error": "after must be a next_cursor string"})
    params: dict[str, str | int] = {
        "visibility": "mine",
        "kind": "default",
        "sort_by": "updated_at",
        "limit": min(limit, MAX_LISTED_SESSIONS),
    }
    if project is not None:
        params["project"] = project.strip()
    if after:
        params["after"] = after
    try:
        projects_response = await client.get("/v1/sessions/projects", timeout=30.0)
        sessions_response = await client.get("/v1/sessions", params=params, timeout=30.0)
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"sys_project_list: server unreachable: {exc}"})
    for response in (projects_response, sessions_response):
        if response.is_error:
            return json.dumps(
                {
                    "error": f"sys_project_list failed: HTTP {response.status_code}",
                    "detail": response.text[:1000],
                }
            )
    summaries = projects_response.json()
    names_by_id = {p["id"]: p["name"] for p in summaries if p.get("id")}
    page = sessions_response.json()
    sessions = []
    for session in page.get("data", []):
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
    return json.dumps(
        {
            "projects": [{"name": p["name"], "id": p.get("id")} for p in summaries],
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


async def _move_one(
    client: httpx.AsyncClient,
    raw_id: str,
    *,
    project_id: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    result: dict[str, Any] = {"session_id": raw_id, "status": "error"}
    try:
        session_id = normalize_session_id(raw_id)
    except ValueError:
        return {**result, "error": "invalid session ID"}
    path = f"/v1/sessions/{session_id}"
    try:
        snapshot = await client.get(path, params=_SNAPSHOT_PARAMS, timeout=30.0)
        if snapshot.is_error:
            return {**result, "error": _refusal(snapshot)}
        current = snapshot.json()
        if current.get("parent_session_id"):
            return {
                **result,
                "error": "sub-agent sessions follow their parent; move the parent session",
            }
        representative = (current.get("family") or {}).get("representative_id")
        if representative and representative != current.get("id"):
            return {
                **result,
                "error": f"this session moves with its family; move {representative} instead",
            }
        labels = current.get("labels") or {}
        if (current.get("project_id") or "") == project_id and not labels.get(PROJECT_LABEL_KEY):
            return {**result, "status": "unchanged"}
        payload = {"project_id": project_id, "labels": {PROJECT_LABEL_KEY: ""}}
        moved = await client.patch(path, json=payload, headers=headers, timeout=30.0)
    except (httpx.HTTPError, ValueError) as exc:
        return {**result, "error": f"move could not be confirmed: {exc}"}
    if moved.is_error:
        return {**result, "error": _refusal(moved)}
    return {**result, "status": "moved"}


async def execute_session_move(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    File each listed session into the named project, or unfile it for ``""``.

    Each move is the sidebar's ``PATCH /v1/sessions/{id}``, which only changes
    folder membership, so a running session keeps its turn. The calling session
    is sent as the orchestration source so the server can refuse moves from a
    session shared with edit access.

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
    name = project.strip()
    project_id, created = "", False
    if name:
        try:
            project_id, created = await resolve_project_id(client, name)
        except (httpx.HTTPError, ValueError) as exc:
            return json.dumps({"error": f"sys_session_move: {exc}"})
    headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
    results = [
        await _move_one(client, raw_id, project_id=project_id, headers=headers)
        for raw_id in dict.fromkeys(session_ids)
    ]
    return json.dumps({"project": name or None, "project_created": created, "results": results})
