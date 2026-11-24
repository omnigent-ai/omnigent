"""Run ticket tools through the same project-private APIs as the web board."""

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.tools.builtins.project_tickets import TICKET_TOOLS
from omnigent.tools.builtins.upload_file import safe_resolve
from omnigent.util.uuid import uuid_to_bytes


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
        if name == "sys_project_identity_create":
            created = await request("POST", f"/v1/projects/{project_id}/identities", json=body)
            return json.dumps(
                {
                    "identity": {key: created[key] for key in ("id", "name", "instructions")},
                    "note": "Created; it appears in the project's Team dialog.",
                },
                ensure_ascii=False,
            )
        if name == "sys_project_identity_update":
            return await _update_identity(body, project_id, request)
        if name == "sys_project_identity_session":
            return await _open_identity_session(body, project_id, request, client, source_id)
        path = f"/v1/projects/{project_id}/tickets"
        if ticket_id is not None:
            path += f"/{ticket_id}"
        guidance = {"include_guidance": "true"} if body.pop("include_guidance", False) else {}
        if name == "sys_ticket_list":
            compact = body.pop("compact", True)
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
                "sys_session_link_ticket": ("POST", "/sessions"),
                "sys_ticket_link": ("POST", "/related"),
            }[name]
            result = await request(method, path + suffix, json=body)
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
    "comment_count",
    "has_running_agents",
    "needs_final_update",
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
    roles = [entry for entry in (await request("GET", base))["data"] if not entry["builtin"]]
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
    if folded == "orchestrator":
        return None, json.dumps({"error": "The Orchestrator is built in, not a project identity"})
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
    updated = await request("PATCH", f"{base}/{identity['id']}", json=body)
    return json.dumps(
        {"identity": {key: updated[key] for key in ("id", "name", "instructions")}},
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
