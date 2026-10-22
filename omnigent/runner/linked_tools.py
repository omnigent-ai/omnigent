"""One REST dispatch shared by SDK and native link tools."""

import json
from typing import Any

import httpx


async def execute_link_tool(
    name: str,
    args: dict[str, Any],
    client: httpx.AsyncClient | None,
    source_id: str | None,
    operation_id: str,
) -> str:
    if client is None or source_id is None:
        return json.dumps({"error": "Session links require an authenticated runner"})
    base = f"/v1/sessions/{source_id}"
    page = {key: args[key] for key in ("after", "limit") if key in args}
    try:
        if name == "sys_session_links":
            response = await client.get(
                f"{base}/communication-activity",
                params={**page, "incoming": args.get("incoming", False)},
            )
        elif name == "sys_session_delegate":
            response = await client.post(
                f"{base}/linked-requests",
                json={
                    "target_id": args.get("target_id"),
                    "message": args.get("message"),
                    "operation_id": operation_id,
                },
                timeout=60,
            )
        elif name == "sys_session_task":
            response = await client.get(
                f"{base}/linked-requests/{args.get('request_id', '')}", params=page
            )
        else:
            response = await client.post(
                f"{base}/linked-requests/{args.get('request_id', '')}/replies",
                json={"message": args.get("message"), "operation_id": operation_id},
            )
    except httpx.HTTPError:
        return json.dumps({"error": "Delivery could not be confirmed; do not resend as new work"})
    if response.is_error:
        return json.dumps(
            {
                "error": "Linked operation was refused",
                "status": response.status_code,
                "detail": response.text[:1000],
            }
        )
    return json.dumps(response.json())
