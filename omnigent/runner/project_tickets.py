"""Run ticket tools through the same project-private APIs as the web board."""

import asyncio
import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.tools.builtins.project_tickets import TICKET_TOOLS
from omnigent.util.uuid import uuid_to_bytes


async def execute_ticket_tool(
    name: str,
    args: dict[str, Any],
    client: httpx.AsyncClient | None,
    source_id: str | None,
) -> str:
    if client is None or source_id is None:
        return json.dumps(
            {"error": "Ticket tools require a project session connected to the server"}
        )
    try:
        body = TICKET_TOOLS[name].arguments.model_validate(args).model_dump(exclude_none=True)
        if name == "sys_ticket_update" and "lead_session_id" in args:
            body["lead_session_id"] = args["lead_session_id"]
        source_id = uuid_to_bytes(source_id).hex()
        ticket_id = uuid_to_bytes(body.pop("ticket_id")).hex() if "ticket_id" in body else None
        if "after" in body and isinstance(body["after"], str):
            body["after"] = uuid_to_bytes(body["after"]).hex()
        if name == "sys_session_link_ticket":
            body["session_id"] = uuid_to_bytes(body.get("session_id", source_id)).hex()
        for key in ("lead_session_id", "related_ticket_id", "sessions_after", "related_after"):
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
        path = f"/v1/projects/{project_id}/tickets"
        if ticket_id is not None:
            path += f"/{ticket_id}"
        if name == "sys_ticket_list":
            result = await request("GET", path, params=body)
        elif name == "sys_ticket_get":
            sessions_after = body.pop("sessions_after", None)
            related_after = body.pop("related_after", None)
            session_params = {"limit": body["limit"]}
            if sessions_after is not None:
                session_params["after"] = sessions_after
            related_params = {"limit": body["limit"]}
            if related_after is not None:
                related_params["after"] = related_after
            ticket, events, sessions, related = await asyncio.gather(
                request("GET", path),
                request("GET", path + "/events", params=body),
                request("GET", path + "/sessions", params=session_params),
                request("GET", path + "/related", params=related_params),
            )
            result = {"ticket": ticket, "events": events, "sessions": sessions, "related": related}
        else:
            method, suffix = {
                "sys_ticket_create": ("POST", ""),
                "sys_ticket_update": ("PATCH", ""),
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
