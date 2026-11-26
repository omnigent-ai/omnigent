"""Run ticket tools through the same project-private APIs as the web board."""

import asyncio
import base64
import json
import mimetypes
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime.mcp_tool_result import encode_mcp_image_result, native_image_payload
from omnigent.tools.builtins.project_tickets import TICKET_TOOLS
from omnigent.tools.builtins.upload_file import safe_resolve
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes


async def _other_project_id(
    wanted: str,
    own_project_id: str,
    request: Callable[..., Awaitable[dict[str, Any]]],
    own_hint: str,
) -> str | dict[str, Any]:
    """The ID of another project shared with the caller's person, or an error to return."""
    wanted = wanted.strip()
    listed = (await request("GET", "/v1/projects")).get("data", [])
    try:
        wanted_id = uuid_to_bytes(wanted).hex()
    except InvalidUuidError:
        wanted_id = None
    matches = [
        project
        for project in listed
        if (wanted_id is not None and project.get("id") == wanted_id)
        or str(project.get("name", "")).casefold() == wanted.casefold()
    ]
    if not matches:
        return {"error": f"No project named {wanted!r} is shared with the person you work for"}
    if len(matches) > 1:
        return {
            "error": f"Several projects are named {wanted!r}; pass one ID instead",
            "project_ids": [project.get("id") for project in matches],
        }
    if matches[0]["id"] == own_project_id:
        return {"error": f"That's this session's own project; {own_hint}"}
    return matches[0]["id"]


async def _file_in_other_project(
    body: dict[str, Any],
    own_project_id: str,
    request: Callable[..., Awaitable[dict[str, Any]]],
) -> str:
    """File an intake ticket on another project's board, found by name or ID."""
    target_id = await _other_project_id(
        body.pop("project"), own_project_id, request, "use sys_ticket_create"
    )
    if isinstance(target_id, dict):
        return json.dumps(target_id)
    created = await request("POST", f"/v1/projects/{target_id}/tickets/intake", json=body)
    return json.dumps(
        {**created, "note": "Filed. That project's Lead triages it under its own rules."},
        ensure_ascii=False,
    )


async def _message_other_lead(
    body: dict[str, Any],
    own_project_id: str,
    request: Callable[..., Awaitable[dict[str, Any]]],
) -> str:
    """Message another project's Lead, or reply to a message one sent this project."""
    message = {"message": body["message"]}
    reply_to = body.get("reply_to")
    if reply_to is not None:
        try:
            thread_id = uuid_to_bytes(reply_to).hex()
        except InvalidUuidError:
            return json.dumps({"error": "reply_to must be the message ID you were sent"})
        path = f"/v1/projects/{own_project_id}/lead-messages/{thread_id}"
        sent = await request("POST", f"{path}/reply", json=message)
    else:
        target_id = await _other_project_id(
            body["project"], own_project_id, request, "use sys_session_message"
        )
        if isinstance(target_id, dict):
            return json.dumps(target_id)
        sent = await request("POST", f"/v1/projects/{target_id}/lead-messages", json=message)
    return json.dumps(
        {
            **sent,
            "note": "Sent. Its reply arrives in this chat as a new message; don't resend "
            "while waiting.",
        },
        ensure_ascii=False,
    )


async def _read_other_board(
    body: dict[str, Any],
    ticket_id: str | None,
    own_project_id: str,
    request: Callable[..., Awaitable[dict[str, Any]]],
) -> str:
    """Read another project's board, when its owner lists this project as a reader."""
    target_id = await _other_project_id(
        body.pop("project"), own_project_id, request, "use sys_ticket_list"
    )
    if isinstance(target_id, dict):
        return json.dumps(target_id)
    action = body.pop("action", "list")
    base = f"/v1/projects/{target_id}/board-reads"
    params = {key: value for key, value in body.items() if key in ("stage_id", "after", "limit")}
    if action == "stages":
        result = await request("GET", f"{base}/stages")
    elif action == "list":
        result = await request("GET", f"{base}/tickets", params=params)
    elif action == "get":
        result = await request("GET", f"{base}/tickets/{ticket_id}")
    else:
        params.pop("stage_id", None)
        result = await request("GET", f"{base}/tickets/{ticket_id}/comments", params=params)
    return json.dumps(result, ensure_ascii=False)


async def execute_ticket_tool(
    name: str,
    args: dict[str, Any],
    client: httpx.AsyncClient | None,
    source_id: str | None,
    workspace: Path | None = None,
) -> str:
    if client is None or source_id is None:
        return json.dumps(
            {"error": "Ticket tools require a project session connected to the server"}
        )
    try:
        body = TICKET_TOOLS[name].arguments.model_validate(args).model_dump(exclude_none=True)
        source_id = uuid_to_bytes(source_id).hex()
        ticket_id = uuid_to_bytes(body.pop("ticket_id")).hex() if "ticket_id" in body else None
        if "after" in body and isinstance(body["after"], str):
            body["after"] = uuid_to_bytes(body["after"]).hex()
        if name == "sys_session_link_ticket":
            body["session_id"] = uuid_to_bytes(body.get("session_id", source_id)).hex()
        for key in ("related_ticket_id", "sessions_after", "related_after"):
            if body.get(key) is not None:
                body[key] = uuid_to_bytes(body[key]).hex()
        headers = {ORCHESTRATION_SOURCE_HEADER: source_id}

        async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            response = await client.request(method, path, headers=headers, timeout=30.0, **kwargs)
            response.raise_for_status()
            return response.json()

        source = await request(
            "GET",
            f"/v1/sessions/{source_id}",
            params={
                "include_items": "false",
                "include_liveness": "false",
                "include_usage": "false",
                "include_live_status": "false",
            },
        )
        if not source.get("project_id"):
            return json.dumps({"error": "File this session into a project before using its board"})
        project_id = uuid_to_bytes(source["project_id"]).hex()
        if name == "sys_project_artifact_save":
            return await _save_artifact(
                body, ticket_id, project_id, workspace, request, client, source_id
            )
        if name == "sys_project_file_comments":
            return await _file_comments(body, project_id, request)
        if name == "sys_project_file_view":
            return await _view_project_file(body, project_id, workspace, client, source_id)
        if name == "sys_project_mandate_propose":
            created = await request(
                "POST", f"/v1/projects/{project_id}/mandate/proposals", json=body
            )
            return json.dumps(
                {
                    "status": "pending_owner_approval",
                    "proposal": created["proposal"],
                    "note": "Not in effect until the project owner accepts it in the "
                    "project's Mandate dialog.",
                },
                ensure_ascii=False,
            )
        if name == "sys_project_telemetry_get":
            role = body.pop("role", None)
            if role is not None:
                identity, error = await _resolve_identity(
                    role, f"/v1/projects/{project_id}/identities", request
                )
                if identity is None:
                    return error
                body["identity_id"] = identity["id"]
            result = await request("GET", f"/v1/projects/{project_id}/telemetry", params=body)
            return json.dumps(result, ensure_ascii=False)
        if name in ("sys_project_kpis_get", "sys_project_kpis_set"):
            path = f"/v1/projects/{project_id}/kpis"
            role = body.pop("role", None)
            if role is not None:
                base = f"/v1/projects/{project_id}/identities"
                identity, error = await _resolve_identity(role, base, request)
                if identity is None:
                    return error
                path = f"{base}/{identity['id']}/kpis"
            if name == "sys_project_kpis_get":
                result = await request("GET", path, params=body)
            else:
                result = await request("PUT", path, json=body)
            return json.dumps(result, ensure_ascii=False)
        if name == "sys_project_identity_list":
            roles = await _list_roles(f"/v1/projects/{project_id}/identities", request)
            return json.dumps(
                {"data": [_role_summary(role) for role in roles]}, ensure_ascii=False
            )
        if name == "sys_project_identity_create":
            return await _create_identity(body, project_id, request)
        if name == "sys_project_identity_update":
            return await _update_identity(body, project_id, request)
        if name == "sys_project_identity_session":
            return await _open_identity_session(body, project_id, request, client, source_id)
        if name == "sys_project_ticket_file":
            return await _file_in_other_project(body, project_id, request)
        if name == "sys_project_lead_message":
            return await _message_other_lead(body, project_id, request)
        if name == "sys_project_board_read":
            return await _read_other_board(body, ticket_id, project_id, request)
        path = f"/v1/projects/{project_id}/tickets"
        if ticket_id is not None:
            path += f"/{ticket_id}"
        guidance = {"include_guidance": "true"} if body.pop("include_guidance", False) else {}
        if name == "sys_ticket_list":
            compact = body.pop("compact", True)
            query = body.pop("query", None)
            if query is not None:
                if body.get("stage_id") is not None or body.get("completed_since") is not None:
                    return json.dumps(
                        {
                            "error": "query searches every stage; omit stage_id and "
                            "completed_since with it"
                        }
                    )
                params = {"q": query, "limit": min(body.get("limit", 20), 100)}
                if body.get("after") is not None:
                    params["after"] = body["after"]
                result = await request("GET", path + "/search", params=params)
            else:
                result = await request("GET", path, params={**body, **guidance})
            if compact:
                result = _compact_page(result)
        elif name == "sys_ticket_get":
            include_description = body.pop("include_description", True)
            include_stages = body.pop("include_stages", True)
            sessions_after = body.pop("sessions_after", None)
            related_after = body.pop("related_after", None)
            session_params = {"limit": body["limit"]}
            if sessions_after is not None:
                session_params["after"] = sessions_after
            related_params = {"limit": body["limit"]}
            if related_after is not None:
                related_params["after"] = related_after
            reads = [
                request("GET", path, params=guidance),
                request("GET", path + "/events", params=body),
                request("GET", path + "/sessions", params=session_params),
                request("GET", path + "/related", params=related_params),
            ]
            if include_stages:
                reads.append(request("GET", f"/v1/projects/{project_id}/tickets/stages"))
            ticket, events, sessions, related, *stages = await asyncio.gather(*reads)
            if not include_description:
                ticket.pop("description", None)
                for event in events.get("data", []):
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        payload.pop("description", None)
                for row in related.get("data", []):
                    row.pop("description", None)
            result = {
                "ticket": ticket,
                "events": events,
                "sessions": sessions,
                "related": related,
                **(stages[0] if stages else {}),
            }
        else:
            method, suffix = {
                "sys_ticket_create": ("POST", ""),
                "sys_ticket_update": ("PATCH", ""),
                "sys_ticket_stages_update": ("PUT", "/stages"),
                "sys_ticket_comment": ("POST", "/comments"),
                "sys_ticket_land": ("POST", "/land"),
                "sys_ticket_push_prefix": ("GET", "/push-prefix"),
                "sys_ticket_revert_draft": ("POST", "/revert-draft"),
                "sys_ticket_import_pr": ("POST", "/import-pr"),
                "sys_session_link_ticket": ("POST", "/sessions"),
                "sys_ticket_link": ("POST", "/related"),
            }[name]
            payload = {"params": body} if method == "GET" else {"json": body}
            result = await request(method, path + suffix, **payload)
        return json.dumps(result, ensure_ascii=False)
    except httpx.HTTPStatusError as exc:
        return json.dumps(
            {
                "error": f"{name} failed: HTTP {exc.response.status_code}",
                "detail": exc.response.text[:1000],
            }
        )
    except (ValueError, httpx.HTTPError) as exc:
        return json.dumps({"error": str(exc)})


# Compact list rows keep what a board scan needs; read one ticket for the rest.
_COMPACT_TICKET_FIELDS = (
    "id",
    "title",
    "stage_id",
    "labels",
    "latest_update",
    "updated_at",
    "completed_at",
    "landed_at",
    "comment_count",
    "has_running_agents",
    "needs_final_update",
    "match",
)
_COMPACT_STAGE_FIELDS = ("id", "name", "is_complete")
_LATEST_UPDATE_MAX_CHARS = 160


def _compact_page(page: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for ticket in page.get("data", []):
        row = {key: ticket[key] for key in _COMPACT_TICKET_FIELDS if key in ticket}
        latest = row.get("latest_update")
        if isinstance(latest, str) and len(latest) > _LATEST_UPDATE_MAX_CHARS:
            row["latest_update"] = latest[: _LATEST_UPDATE_MAX_CHARS - 1].rstrip() + "…"
        rows.append(row)
    compact = {key: value for key, value in page.items() if key not in ("data", "stages")}
    compact["stages"] = [
        {key: stage[key] for key in _COMPACT_STAGE_FIELDS if key in stage}
        for stage in page.get("stages", [])
    ]
    compact["data"] = rows
    return compact


async def _list_roles(base: str, request: Any) -> list[dict[str, Any]]:
    """The project's identities other than the built-in Lead."""
    return [entry for entry in (await request("GET", base))["data"] if not entry["builtin"]]


def _role_summary(role: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": role["id"],
        "name": role["name"],
        "instructions": role.get("instructions", ""),
        "scope": role.get("scope"),
        "created_by": "agent" if role.get("created_by_session_id") else "owner",
        "session_id": role.get("active_session_id"),
        "session_state": role.get("active_session_state"),
    }


def _with_scope(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fold the tools' ``owns``/``does_not_own`` lists into the API's ``scope``.

    A list left out keeps its *current* items, so updating one doesn't erase the other.
    """
    owns, excludes = body.pop("owns", None), body.pop("does_not_own", None)
    if owns is not None or excludes is not None:
        current = current or {}
        body["scope"] = {
            "owns": owns if owns is not None else current.get("owns") or [],
            "excludes": excludes if excludes is not None else current.get("excludes") or [],
        }
    return body


async def _create_identity(body: dict[str, Any], project_id: str, request: Any) -> str:
    """Create a role, or return the same-named one so retries and rollovers never duplicate."""
    body = _with_scope(body)
    base = f"/v1/projects/{project_id}/identities"
    try:
        created = await request("POST", base, json=body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        folded = body["name"].strip().casefold()
        existing = next(
            (
                role
                for role in await _list_roles(base, request)
                if role["name"].casefold() == folded
            ),
            None,
        )
        if existing is None:
            raise
        differ = existing.get("instructions", "").strip() != body["instructions"].strip()
        note = "An identity with this name already exists; nothing was created or changed. "
        if not differ:
            note += "Its persona matches yours."
        elif existing.get("created_by_session_id"):
            note += "Its persona differs from yours; sys_project_identity_update can replace it."
        else:
            note += "Its persona differs from yours; the owner made it, so only they can edit it."
        return json.dumps(
            {
                "identity": _role_summary(existing),
                "existing": True,
                "instructions_differ": differ,
                "note": note,
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "identity": {key: created[key] for key in ("id", "name", "instructions")},
            "existing": False,
            "note": "Created; it appears in the project's Team dialog.",
        },
        ensure_ascii=False,
    )


async def _resolve_identity(
    wanted: str, base: str, request: Any
) -> tuple[dict[str, Any] | None, str]:
    """Find a non-built-in identity by ID or case-insensitive name, or explain the miss."""
    wanted = wanted.strip()
    folded = wanted.casefold()
    try:
        wanted_id = uuid_to_bytes(wanted).hex()
    except ValueError:
        wanted_id = None
    roles = await _list_roles(base, request)
    identity = next(
        (
            entry
            for entry in roles
            if entry["id"] == wanted_id or entry["name"].casefold() == folded
        ),
        None,
    )
    if identity is not None:
        return identity, ""
    if folded in ("lead", "orchestrator"):
        return None, json.dumps({"error": "The Lead is built in, not a project identity"})
    return None, json.dumps(
        {
            "error": f"No identity named {wanted!r}; create one with sys_project_identity_create",
            "identities": [entry["name"] for entry in roles],
        },
        ensure_ascii=False,
    )


async def _update_identity(body: dict[str, Any], project_id: str, request: Any) -> str:
    """Rename or re-persona an agent-created identity; the server refuses owner-made ones."""
    base = f"/v1/projects/{project_id}/identities"
    identity, error = await _resolve_identity(body.pop("identity"), base, request)
    if identity is None:
        return error
    body = _with_scope(body, identity.get("scope"))
    updated = await request("PATCH", f"{base}/{identity['id']}", json=body)
    return json.dumps(
        {"identity": {key: updated.get(key) for key in ("id", "name", "instructions", "scope")}},
        ensure_ascii=False,
    )


async def _open_identity_session(
    body: dict[str, Any],
    project_id: str,
    request: Any,
    client: httpx.AsyncClient,
    source_id: str,
) -> str:
    """Start or reuse an existing identity's single session, then optionally message it."""
    from omnigent.runner.session_message import execute_message_tool

    base = f"/v1/projects/{project_id}/identities"
    identity, error = await _resolve_identity(body["identity"], base, request)
    if identity is None:
        return error
    opened = await request(
        "POST",
        f"{base}/{identity['id']}/session",
        json={"expected_session_id": identity["active_session_id"]},
    )
    result: dict[str, Any] = {
        "identity": {"id": identity["id"], "name": identity["name"]},
        "session_id": opened["session_id"],
        "created": opened["created"],
        "previous_state": identity["active_session_state"],
    }
    if "message" in body:
        delivery = await execute_message_tool(
            {"session_id": opened["session_id"], "message": body["message"]}, client, source_id
        )
        result["message_delivery"] = json.loads(delivery)
    return json.dumps(result, ensure_ascii=False)


async def _save_artifact(
    body: dict[str, Any],
    ticket_id: str | None,
    project_id: str,
    workspace: Path | None,
    request: Any,
    client: httpx.AsyncClient,
    source_id: str,
) -> str:
    if workspace is None:
        return json.dumps({"error": "sys_project_artifact_save needs a session workspace"})
    # The runner reads outside any sandbox, so confine the path to the workspace.
    resolved = safe_resolve(body["path"], workspace)
    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {body['path']}"})
    content = await asyncio.to_thread(resolved.read_bytes)
    content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    if resolved.suffix.lower() == ".md":
        content_type = "text/markdown"
    form = {key: body[key] for key in ("title",) if body.get(key)}
    if ticket_id is not None:
        form["ticket_id"] = ticket_id
    headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
    response = await client.post(
        f"/v1/projects/{project_id}/files",
        files={"file": (resolved.name, content, content_type)},
        data=form,
        headers=headers,
        timeout=60.0,
    )
    response.raise_for_status()
    artifact = response.json()
    result: dict[str, Any] = {"artifact": artifact}
    if ticket_id is not None:
        text = body.get("comment") or f"Attached `{artifact['title']}`."
        result["comment"] = await request(
            "POST",
            f"/v1/projects/{project_id}/tickets/{ticket_id}/comments",
            json={"text": text, "outputs": [{"type": "artifact", "url": artifact["url"]}]},
        )
    return json.dumps(result, ensure_ascii=False)


_COMMENT_FIELDS = ("id", "created_by", "anchor_content", "body", "status", "created_at")


async def _file_comments(
    body: dict[str, Any],
    project_id: str,
    request: Callable[..., Awaitable[dict[str, Any]]],
) -> str:
    """List, reply to, resolve or reopen comments on one of the project's files."""
    file_id = uuid_to_bytes(body["file_id"]).hex()
    base = f"/v1/projects/{project_id}/files/{file_id}/comments"
    action = body["action"]
    listed = (await request("GET", base))["comments"]
    if action == "list":
        rows = [
            {key: row.get(key) for key in _COMMENT_FIELDS}
            for row in listed
            if body.get("include_resolved") or row.get("status") == "draft"
        ]
        return json.dumps({"comments": rows}, ensure_ascii=False)
    comment = next((row for row in listed if row["id"] == body.get("comment_id")), None)
    if comment is None:
        return json.dumps({"error": "No such comment on this file; list them first"})
    if action == "reply":
        if not body.get("text"):
            return json.dumps({"error": "reply needs text"})
        reply = await request(
            "POST",
            base,
            json={
                "path": comment["path"],
                "body": body["text"],
                "start_index": comment["start_index"],
                "end_index": comment["end_index"],
                "anchor_content": comment.get("anchor_content"),
            },
        )
        # A reply answers the thread; it isn't new work for the person or the next agent.
        reply = await request("PATCH", f"{base}/{reply['id']}", json={"status": "addressed"})
        return json.dumps({key: reply.get(key) for key in _COMMENT_FIELDS}, ensure_ascii=False)
    status = "addressed" if action == "resolve" else "draft"
    updated = await request("PATCH", f"{base}/{comment['id']}", json={"status": status})
    return json.dumps({key: updated.get(key) for key in _COMMENT_FIELDS}, ensure_ascii=False)


# Project file content paths agents meet in ticket text and outputs.
_PROJECT_FILE_PATH = re.compile(
    r"^(?:https?://[^/]+)?(?:/[^?#]*?)?/v1/projects/([^/?#]+)/(ticket-attachments|files)/"
    r"([^/?#]+)/content/?(?:[?#].*)?$"
)
# Text a viewer can read inline without flooding the context.
_MAX_TEXT_CHARS = 50_000
_TEXT_TYPES = frozenset({"application/json", "application/xml", "text/markdown", "text/csv"})


async def _view_project_file(
    body: dict[str, Any],
    project_id: str,
    workspace: Path | None,
    client: httpx.AsyncClient,
    source_id: str,
) -> str:
    """Read a ticket attachment or project file of the session's own project.

    The read goes through the runner's server client, so the server applies the
    same project access check as the board; a path naming another project is
    refused before any request.
    """
    match = _PROJECT_FILE_PATH.match(body["url"].strip())
    if match is None:
        return json.dumps(
            {
                "error": "Not a project file path. Expected /v1/projects/<project>/"
                "ticket-attachments/<id>/content or /v1/projects/<project>/files/<id>/content."
            }
        )
    raw_project, kind, raw_file = match.groups()
    try:
        if uuid_to_bytes(raw_project).hex() != project_id:
            return json.dumps({"error": "That file belongs to another project"})
        file_id = uuid_to_bytes(raw_file).hex()
    except ValueError:
        return json.dumps({"error": "Not a project file path"})
    save_target: Path | None = None
    if body.get("save_path"):
        if workspace is None:
            return json.dumps({"error": "save_path needs a session workspace"})
        save_target = safe_resolve(body["save_path"], workspace)
    response = await client.get(
        f"/v1/projects/{project_id}/{kind}/{file_id}/content",
        headers={ORCHESTRATION_SOURCE_HEADER: source_id},
        timeout=60.0,
    )
    if response.status_code == 404:
        return json.dumps({"error": "File not found in this project"})
    response.raise_for_status()
    content = response.content
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    metadata: dict[str, Any] = {
        "url": f"/v1/projects/{project_id}/{kind}/{file_id}/content",
        "content_type": content_type or None,
        "bytes": len(content),
    }
    if save_target is not None:
        try:
            await asyncio.to_thread(save_target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(save_target.write_bytes, content)
            metadata["saved_path"] = str(save_target)
        except OSError as exc:
            metadata["save_error"] = f"Couldn't save the file: {exc}"
    if content_type.startswith("image/"):
        image = native_image_payload(base64.b64encode(content).decode("ascii"), content_type)
        if image is not None:
            return encode_mcp_image_result(
                [
                    {"type": "text", "text": json.dumps(metadata)},
                    {"type": "image", "mimeType": content_type, "data": image},
                ],
                is_error=False,
            )
        metadata["note"] = (
            "The image couldn't be decoded for viewing; use save_path to inspect it."
        )
    elif content_type.startswith("text/") or content_type in _TEXT_TYPES:
        text = content.decode("utf-8", errors="replace")
        metadata["truncated"] = len(text) > _MAX_TEXT_CHARS
        metadata["text"] = text[:_MAX_TEXT_CHARS]
    elif save_target is None:
        metadata["note"] = "Binary file; pass save_path to write it into your workspace."
    return json.dumps(metadata, ensure_ascii=False)
