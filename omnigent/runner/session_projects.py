"""Organize the owner's projects and existing sessions via the sidebar's APIs."""

import asyncio
import json
import time
from typing import Any

import httpx

from omnigent.entities.conversation import USER_SESSION_TITLE_MAX_CHARS
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_requests import normalize_session_id
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store import PINNED_LABEL_KEY, PROJECT_LABEL_KEY
from omnigent.tools.builtins.session_projects import (
    MAX_LISTED_SESSIONS,
    MAX_PROJECT_ICON_LENGTH,
    MAX_PROJECT_NAME_LENGTH,
    MAX_SEARCH_LENGTH,
)

_DEFAULT_LISTED_SESSIONS = 50
_SNAPSHOT_PARAMS = {"include_items": "false", "include_liveness": "false"}
# The shared-runner check reads only the caller's runner binding.
_CALLER_RUNNER_PARAMS = {
    **_SNAPSHOT_PARAMS,
    "include_usage": "false",
    "include_live_status": "false",
}
_CONCURRENT_NAME_READS = 8
_SESSION_UPDATE_FIELDS = ("project", "title", "pinned", "archived")
# Upper bound on pages walked when resolving a project or listing its members.
_MAX_LOOKUP_PAGES = 20
# /v1/info flags: the first covers only project filing, the second every other
# organizing write (session title/pin/archive, project rename/icon/delete).
_MOVE_SOURCE_CHECKS = "session_move_source_checks"
_ORGANIZE_SOURCE_CHECKS = "session_organize_source_checks"


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


async def _find_project(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    """Return the caller's project named exactly *name*, or ``None``, reading bounded pages."""
    after: str | None = None
    for _ in range(_MAX_LOOKUP_PAGES):
        params: dict[str, str | int] = {"limit": MAX_LISTED_SESSIONS}
        if after:
            params["after"] = after
        resp = await client.get("/v1/projects", params=params, timeout=30.0)
        if resp.status_code >= 400:
            raise ValueError(f"could not list projects: {resp.text[:200]}")
        page = resp.json()
        match = next(
            (p for p in page.get("data", []) if p.get("name") == name and p.get("id")), None
        )
        if match is not None:
            return match
        after = page.get("last_id")
        if not page.get("has_more") or not after:
            return None
    raise ValueError("could not list projects: too many to search")


async def _project_members(
    client: httpx.AsyncClient, name: str
) -> tuple[list[dict[str, Any]], bool]:
    """
    Return the owner's sessions filed under *name*, archived included, and whether
    the walk finished.

    The sessions route drops rows (side chats) after paging, so an empty page
    with ``has_more`` proves nothing; the walk follows the cursor to the end.
    """
    members: dict[str, dict[str, Any]] = {}
    after: str | None = None
    for _ in range(_MAX_LOOKUP_PAGES):
        params: dict[str, str | int] = {
            "visibility": "mine",
            "include_archived": "true",
            "project": name,
            "limit": MAX_LISTED_SESSIONS,
        }
        if after:
            params["after"] = after
        resp = await client.get("/v1/sessions", params=params, timeout=30.0)
        if resp.is_error:
            raise ValueError(f"could not list the project's sessions: HTTP {resp.status_code}")
        page = resp.json()
        for row in page.get("data", []):
            if isinstance(row.get("id"), str):
                members[row["id"]] = row
        after = page.get("last_id")
        if not page.get("has_more") or not after:
            return list(members.values()), True
    return list(members.values()), False


async def _refile_members(
    client: httpx.AsyncClient,
    members: list[dict[str, Any]],
    *,
    project_id: str,
    headers: dict[str, str],
) -> tuple[int, list[str]]:
    """
    File each member into *project_id* (``""`` unfiles) and clear its legacy label.

    Matches the sidebar's rename and delete sweeps: only family representatives
    are patched, since the rest of a family follows them.

    :returns: ``(moved, failed)``: how many sessions were patched, and the ids
        that could not be.
    """
    moved = 0
    failed: list[str] = []
    for member in members:
        representative = (member.get("family") or {}).get("representative_id")
        if representative and representative != member["id"]:
            continue
        labels = member.get("labels") or {}
        if (member.get("project_id") or "") == project_id and not labels.get(PROJECT_LABEL_KEY):
            continue
        try:
            response = await client.patch(
                f"/v1/sessions/{member['id']}",
                json={"project_id": project_id, "labels": {PROJECT_LABEL_KEY: ""}},
                headers=headers,
                timeout=30.0,
            )
            if response.is_error:
                failed.append(member["id"])
            else:
                moved += 1
        except httpx.HTTPError:
            failed.append(member["id"])
    return moved, failed


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

    :param args: Tool arguments: optional ``project``, ``search``, ``pinned``,
        ``include_archived``, ``limit``, ``after`` and ``projects_after``.
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
    search = args.get("search")
    if search is not None and (not isinstance(search, str) or len(search) > MAX_SEARCH_LENGTH):
        return json.dumps(
            {"error": f"search must be text of at most {MAX_SEARCH_LENGTH} characters"}
        )
    flags = {key: args.get(key) for key in ("pinned", "include_archived")}
    if any(value is not None and not isinstance(value, bool) for value in flags.values()):
        return json.dumps({"error": "pinned and include_archived must be true or false"})
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
    if search and search.strip():
        session_params["search_query"] = search.strip()
    if flags["pinned"]:
        session_params["pinned"] = "true"
    if flags["include_archived"]:
        session_params["include_archived"] = "true"
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
        if (session.get("labels") or {}).get(PINNED_LABEL_KEY):
            entry["pinned"] = True
        if session.get("archived") is True:
            entry["archived"] = True
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
    if response.status_code == 404 and detail == "Project not found":
        return "the project no longer exists; nothing was changed"
    if response.status_code == 404:
        return f"session not found or not visible to you ({detail})"
    if response.status_code == 409 and "representative" in detail:
        return "this session shares its family's project; update the moves_with session instead"
    return f"HTTP {response.status_code}: {detail}"


async def _updatable_snapshot(
    client: httpx.AsyncClient, session_id: str, *, set_project: bool
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(snapshot, None)`` for a session this tool may update, else ``(None, error)``."""
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
    # Refuse here so a readable foreign session is never "unchanged".
    if isinstance(level, int) and level < LEVEL_OWNER:
        if set_project:
            return None, "only the session's owner can change its project"
        return None, "only the session's owner can update it"
    parent = current.get("parent_session_id")
    if parent:
        if set_project:
            return (
                None,
                f"sub-agent sessions follow their parent's project; update {parent} instead",
            )
        return None, f"sub-agent sessions follow their parent; update {parent} instead"
    representative = (current.get("family") or {}).get("representative_id")
    if set_project and representative and representative != current.get("id"):
        return None, f"this session shares its family's project; update {representative} instead"
    return current, None


def _session_changes(
    current: dict[str, Any], changes: dict[str, Any], project_id: str | None
) -> dict[str, Any]:
    """Build the sidebar PATCH body for the requested fields that differ from *current*."""
    labels = current.get("labels") or {}
    payload: dict[str, Any] = {}
    patch_labels: dict[str, str] = {}
    if project_id is not None and (
        (current.get("project_id") or "") != project_id or labels.get(PROJECT_LABEL_KEY)
    ):
        payload["project_id"] = project_id
        patch_labels[PROJECT_LABEL_KEY] = ""
    if "title" in changes and current.get("title") != changes["title"]:
        payload["title"] = changes["title"]
    if "pinned" in changes and bool(labels.get(PINNED_LABEL_KEY)) != changes["pinned"]:
        # The sidebar stores the pin time, which orders the pinned section.
        patch_labels[PINNED_LABEL_KEY] = str(int(time.time() * 1000)) if changes["pinned"] else ""
    if "archived" in changes and (current.get("archived") is True) != changes["archived"]:
        payload["archived"] = changes["archived"]
    if patch_labels:
        payload["labels"] = patch_labels
    return payload


async def _patch_session(
    client: httpx.AsyncClient,
    session_id: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
) -> tuple[str, str | None]:
    """Return ``(status, error)`` after applying *payload*; an empty one is ``unchanged``."""
    if not payload:
        return "unchanged", None
    try:
        updated = await client.patch(
            f"/v1/sessions/{session_id}", json=payload, headers=headers, timeout=30.0
        )
    except httpx.HTTPError as exc:
        return "error", f"update could not be confirmed: {exc}"
    if updated.is_error:
        return "error", _refusal(updated)
    return "updated", None


async def _server_checks_source(client: httpx.AsyncClient, flag: str) -> bool:
    """Whether ``/v1/info`` says the server refuses these writes from an edit-shared source."""
    try:
        info = await client.get("/v1/info", timeout=30.0)
        body = info.json() if info.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(body, dict) and body.get(flag) is True


def _unchecked_server_error(tool_name: str, where: str) -> str:
    return json.dumps(
        {
            "error": f"{tool_name}: this Omnigent server cannot check the calling "
            f"session's sharing before changing {where}; upgrade the server or use "
            "the sidebar"
        }
    )


async def _shares_runner(
    client: httpx.AsyncClient, target: dict[str, Any], source_id: str
) -> bool:
    """Whether archiving *target* would stop the calling session's runner too.

    A side chat can run on its parent's runner, so the archive stop of one
    tears down the other. An unreadable caller counts as shared.
    """
    runner_id = target.get("runner_id")
    if not runner_id:
        return False
    try:
        response = await client.get(
            f"/v1/sessions/{source_id}", params=_CALLER_RUNNER_PARAMS, timeout=30.0
        )
        if response.is_error:
            return True
        return response.json().get("runner_id") == runner_id
    except (httpx.HTTPError, ValueError, AttributeError):
        return True


def _validated_session_changes(args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Return the requested ``title``/``pinned``/``archived`` values, or an error."""
    changes: dict[str, Any] = {}
    if "title" in args:
        title = args["title"]
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title.strip()) > USER_SESSION_TITLE_MAX_CHARS
        ):
            return {}, (
                f"title must be non-blank text of at most {USER_SESSION_TITLE_MAX_CHARS} "
                "characters"
            )
        changes["title"] = title.strip()
    for key in ("pinned", "archived"):
        if key in args:
            if not isinstance(args[key], bool):
                return {}, f"{key} must be true or false"
            changes[key] = args[key]
    if changes.get("archived") is True and changes.get("pinned") is True:
        return {}, "an archived session cannot be pinned; pass only archived"
    return changes, None


async def execute_session_update(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    Update one of the owner's sessions: its project, title, pin or archive state.

    Every field goes through the sidebar's ``PATCH /v1/sessions/{id}`` in one
    request. Filing, renaming and pinning only change how the session is listed,
    so a running session keeps its turn; archiving stops its runner. The calling
    session may archive itself, which the server tears down only after this turn
    ends, but not another session whose teardown would stop its runner mid-turn.
    The calling session is sent as the orchestration source on every write, so
    the server refuses both the update and any project creation from a session
    shared with edit access. A project is created only after the session is
    known to be updatable.

    :param args: Tool arguments: ``session_id`` plus any of ``project``
        (``""`` unfiles), ``title``, ``pinned`` and ``archived``.
    :param client: HTTP client pointed at the Omnigent server.
    :param source_id: The calling session.
    :returns: JSON with ``session_id``, ``status`` and each requested field
        (``project`` adds ``project_created``), or ``session_id`` and ``error``.
    """
    session_id = args.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return json.dumps({"error": "session_id must be a session ID"})
    requested = [key for key in _SESSION_UPDATE_FIELDS if key in args]
    if not requested:
        return json.dumps(
            {
                "session_id": session_id,
                "error": "nothing to update: pass project, title, pinned or archived",
            }
        )
    set_project = "project" in args
    project = args.get("project")
    # Only an exact "" unfiles; a blank-looking name must not unfile the session.
    if set_project and (
        not isinstance(project, str)
        or len(project) > MAX_PROJECT_NAME_LENGTH
        or (project and not project.strip())
    ):
        return json.dumps(
            {
                "session_id": session_id,
                "error": 'project must be a name of at most 100 characters, or "" to unfile',
            }
        )
    changes, invalid = _validated_session_changes(args)
    if invalid is not None:
        return json.dumps({"session_id": session_id, "error": invalid})
    try:
        canonical = normalize_session_id(session_id)
    except ValueError:
        return json.dumps({"session_id": session_id, "error": "invalid session ID"})
    if client is None or source_id is None:
        return json.dumps({"error": "sys_session_update requires an authenticated runner"})
    archives_itself = changes.get("archived") is True and _same_session(canonical, source_id)
    flag = _MOVE_SOURCE_CHECKS if requested == ["project"] else _ORGANIZE_SOURCE_CHECKS
    if not await _server_checks_source(client, flag):
        return _unchecked_server_error("sys_session_update", "a session")
    current, error = await _updatable_snapshot(client, canonical, set_project=set_project)
    if current is None:
        return json.dumps({"session_id": session_id, "error": error})
    if (
        changes.get("archived") is True
        and not archives_itself
        and await _shares_runner(client, current, source_id)
    ):
        return json.dumps(
            {
                "session_id": session_id,
                "error": "this session shares the calling session's runner; archiving it "
                "would stop this session too",
            }
        )
    headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
    name = project.strip() if isinstance(project, str) else ""
    project_id: str | None = None
    created = False
    if set_project:
        project_id = ""
        if name and changes.get("archived") is True:
            # The server can still refuse the archive (its lineage guard), and a
            # project created before that refusal would be left behind empty.
            try:
                existing = await _find_project(client, name)
            except (httpx.HTTPError, ValueError) as exc:
                return json.dumps(
                    {"session_id": session_id, "error": f"sys_session_update: {exc}"}
                )
            if existing is None:
                return json.dumps(
                    {
                        "session_id": session_id,
                        "error": f"no project named {name!r}; archive the session and "
                        "file it into a new project in separate calls",
                    }
                )
            # Never fall through to create-if-missing: a project deleted after
            # this read must fail the PATCH, not be recreated.
            project_id = str(existing["id"])
        elif name:
            try:
                project_id, created = await resolve_project_id(client, name, headers=headers)
            except (httpx.HTTPError, ValueError) as exc:
                return json.dumps(
                    {"session_id": session_id, "error": f"sys_session_update: {exc}"}
                )
    payload = _session_changes(current, changes, project_id)
    status, error = await _patch_session(client, canonical, payload, headers=headers)
    if error is not None:
        failed: dict[str, Any] = {"session_id": session_id, "error": error}
        if created:
            # The project was created before the refused PATCH and still exists.
            failed.update(project=name, project_created=True)
        return json.dumps(failed)
    result: dict[str, Any] = {"session_id": session_id, "status": status}
    if set_project:
        result["project"] = name or None
        result["project_created"] = created
    result.update(changes)
    if archives_itself and status == "updated":
        result["note"] = (
            "this session is archived; its runner stops once this turn ends, so finish "
            "the turn now"
        )
    return json.dumps(result)


def _project_name_arg(value: Any) -> str | None:
    """Return the trimmed project name in *value*, or ``None`` when it is invalid."""
    if not isinstance(value, str) or len(value) > MAX_PROJECT_NAME_LENGTH:
        return None
    trimmed = value.strip()
    return trimmed or None


def _project_refusal(response: httpx.Response, name: str | None) -> str:
    if response.status_code == 404:
        return "project not found"
    if response.status_code == 409 and name is not None:
        return f"a project named {name!r} already exists"
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else body.get("detail")
    except (ValueError, AttributeError):
        detail = None
    return f"HTTP {response.status_code}: {detail or response.text[:300]}"


async def _promote_label_folder(
    client: httpx.AsyncClient,
    name: str,
    config: dict[str, Any],
    *,
    reuse_existing: bool,
    headers: dict[str, str],
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Create the project row a label-only folder becomes, or return the error.

    A 409 means the name is taken. Without a rename that is a concurrent
    promotion of this same folder, whose row is reused; with one, the name
    belongs to another project, and reusing it would merge the two folders.
    """
    created = await client.post(
        "/v1/projects", json={"name": name, "config": config}, headers=headers, timeout=30.0
    )
    if created.status_code < 400:
        return created.json(), None
    if created.status_code == 409:
        existing = await _find_project(client, name) if reuse_existing else None
        if existing is not None:
            return existing, None
        return None, f"a project named {name!r} already exists"
    return None, _project_refusal(created, name)


_NO_SUCH_PROJECT = "no project named {name!r}; sys_project_list shows the user's projects"


async def execute_project_update(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    Rename one of the owner's projects or change its emoji icon.

    Uses the sidebar's ``PATCH /v1/projects/{id}``. The icon lives in the
    project's ``config``, which that PATCH replaces whole, so the other stored
    defaults are carried over.

    :param args: Tool arguments: ``project`` (current name), optional ``name``
        and ``icon`` (``""`` removes it).
    :param client: HTTP client pointed at the Omnigent server.
    :param source_id: The calling session, sent as the orchestration source.
    :returns: JSON with ``project``, ``status`` and each requested field, or ``error``.
    """
    current_name = _project_name_arg(args.get("project"))
    if current_name is None:
        return json.dumps({"error": "project must be a project name of at most 100 characters"})
    if "name" not in args and "icon" not in args:
        return json.dumps(
            {"project": current_name, "error": "nothing to update: pass name or icon"}
        )
    new_name = None
    if "name" in args:
        new_name = _project_name_arg(args["name"])
        if new_name is None:
            return json.dumps(
                {
                    "project": current_name,
                    "error": "name must be non-blank text of at most 100 characters",
                }
            )
    icon = args.get("icon")
    if "icon" in args:
        if not isinstance(icon, str) or len(icon.strip()) > MAX_PROJECT_ICON_LENGTH:
            return json.dumps(
                {
                    "project": current_name,
                    "error": f"icon must be one emoji of at most {MAX_PROJECT_ICON_LENGTH} "
                    'characters, or "" to remove it',
                }
            )
        icon = icon.strip()
    if client is None or source_id is None:
        return json.dumps({"error": "sys_project_update requires an authenticated runner"})
    if not await _server_checks_source(client, _ORGANIZE_SOURCE_CHECKS):
        return _unchecked_server_error("sys_project_update", "a project")
    headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
    try:
        project = await _find_project(client, current_name)
    except (httpx.HTTPError, ValueError) as exc:
        # Only a lookup that finished may conclude the project is missing; a
        # failed one must not promote a label folder over a real project.
        return json.dumps({"project": current_name, "error": f"sys_project_update: {exc}"})
    error = _NO_SUCH_PROJECT.format(name=current_name)
    promoted = False
    if project is None:
        # A folder can exist only through its sessions' legacy label. Like the
        # sidebar, promote it by creating the row under its final name and icon,
        # so a refused create leaves nothing behind.
        try:
            members, _ = await _project_members(client, current_name)
            if not members:
                return json.dumps({"project": current_name, "error": error})
            project, error = await _promote_label_folder(
                client,
                new_name or current_name,
                {"icon": icon} if icon else {},
                reuse_existing=new_name is None or new_name == current_name,
                headers=headers,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return json.dumps({"project": current_name, "error": f"sys_project_update: {exc}"})
        if project is None:
            return json.dumps({"project": current_name, "error": error})
        promoted = True
    payload: dict[str, Any] = {}
    if new_name is not None and new_name != project.get("name"):
        payload["name"] = new_name
    if "icon" in args:
        stored = project.get("config") if isinstance(project.get("config"), dict) else {}
        config = {key: value for key, value in stored.items() if key != "icon"}
        if icon:
            config["icon"] = icon
        if config != stored:
            payload["config"] = config
    status = "updated" if promoted else "unchanged"
    if payload:
        try:
            response = await client.patch(
                f"/v1/projects/{project['id']}",
                json=payload,
                headers=headers,
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            return json.dumps(
                {"project": current_name, "error": f"update could not be confirmed: {exc}"}
            )
        if response.is_error:
            return json.dumps(
                {"project": current_name, "error": _project_refusal(response, new_name)}
            )
        status = "updated"
    renamed = new_name is not None and new_name != current_name
    if not (renamed or promoted):
        # The name is unchanged, so label members still match this folder.
        result: dict[str, Any] = {"project": current_name, "status": status}
        if "icon" in args:
            result["icon"] = icon or None
        return json.dumps(result)
    # Sessions that still belong through the legacy label would stay behind in a
    # folder under the old name, so file them into this project's row.
    project_id = str(project["id"])
    try:
        listed, complete = await _project_members(client, current_name)
    except (httpx.HTTPError, ValueError) as exc:
        return json.dumps(
            {
                "project": new_name or current_name,
                "status": "partial",
                "error": f"project updated, but its sessions could not be checked: {exc}",
            }
        )
    # The old name can already belong to another project, whose sessions the
    # listing also returns; only label members not filed elsewhere move.
    stragglers = [
        member
        for member in listed
        if (member.get("labels") or {}).get(PROJECT_LABEL_KEY) == current_name
        and (member.get("project_id") or "") in ("", project_id)
    ]
    moved, not_moved = await _refile_members(
        client, stragglers, project_id=project_id, headers=headers
    )
    if moved:
        status = "updated"
    if not_moved or not complete:
        status = "partial"
    result = {"project": new_name or current_name, "status": status}
    if not_moved:
        result["sessions_not_moved"] = not_moved
    if not complete:
        result["warning"] = (
            "the project has too many sessions to check them all; list the rest with "
            f"sys_project_list project={current_name!r} and move each with sys_session_update"
        )
    if new_name is not None and new_name != current_name:
        result["previous_name"] = current_name
    if "icon" in args:
        result["icon"] = icon or None
    return json.dumps(result)
