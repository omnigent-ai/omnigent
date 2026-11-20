"""Runner-side handlers for the ``sys_thingy_*`` tools.

The runner has no thingy store, so each tool calls the server's ``/v1/thingies``
routes as the session's owner, naming the calling session in the orchestration
header so the server can refuse a call from a shared session. ``sys_thingy_records``
goes through the same data bridge the sandboxed frame uses (``POST .../rpc``),
against the thingy's published revision. Results come back as the routes' JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.tools.builtins.thingies import THINGY_RECORD_OPS, THINGY_TOOL_NAMES

__all__ = ["THINGY_TOOL_NAMES", "execute_thingy_tool"]

_BASE = "/v1/thingies"
_TIMEOUT = 30.0
_THINGY_ID = re.compile(r"[0-9a-f]{32}")
# What a server answers for a path it has no route for: FastAPI's default body, or
# the web UI catch-all's generic error.
_UNROUTED_NOT_FOUND_BODIES = (
    {"detail": "Not Found"},
    {"error": {"code": "not_found", "message": "Not found"}},
)
_UNSUPPORTED = "this Omnigent server has no thingies"


class _ToolError(Exception):
    """A tool failure reported to the agent as ``{"error": ...}``."""

    def __init__(self, error: Any) -> None:
        super().__init__(str(error))
        self.error = error


def _segment(value: Any, label: str) -> str:
    """*value* as one URL path segment."""
    if not isinstance(value, str) or not value:
        raise _ToolError(f"invalid_input: {label} is required")
    return quote(value, safe="")


def _thingy_id(args: dict[str, Any]) -> str:
    value = args.get("thingy_id")
    if not isinstance(value, str) or _THINGY_ID.fullmatch(value) is None:
        raise _ToolError("invalid_input: thingy_id must be a thingy's ID from sys_thingy_list")
    return value


def _operation_id(args: dict[str, Any], call_id: str | None) -> str:
    """The agent's operation ID, or one derived from the tool call so its retries match."""
    given = args.get("operation_id")
    if isinstance(given, str) and given:
        return given
    seed = call_id or uuid.uuid4().hex
    return "tool-" + hashlib.sha256(seed.encode()).hexdigest()[:40]


def _is_unrouted(response: httpx.Response) -> bool:
    if response.status_code == 405:
        return True
    if response.status_code != 404:
        return False
    try:
        return response.json() in _UNROUTED_NOT_FOUND_BODIES
    except ValueError:
        return False


class _Server:
    """The session owner's calls to the thingy routes."""

    def __init__(self, client: httpx.AsyncClient, conversation_id: str) -> None:
        self._client = client
        self._headers = {ORCHESTRATION_SOURCE_HEADER: conversation_id}

    async def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """The route's JSON, or raise :class:`_ToolError` with the server's error."""
        try:
            response = await self._client.request(
                method,
                f"{_BASE}{path}",
                json=body,
                params={key: value for key, value in (params or {}).items() if value is not None}
                or None,
                headers=self._headers,
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise _ToolError(f"could not reach the server: {exc}") from exc
        if _is_unrouted(response):
            raise _ToolError(_UNSUPPORTED)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("message") == "Not found":
                # The thingies feature is off on this server.
                raise _ToolError(_UNSUPPORTED)
            raise _ToolError(error if error is not None else response.text[:500])
        return payload


async def _record(
    server: _Server, args: dict[str, Any], thingy_id: str, call_id: str | None
) -> Any:
    op = args.get("op")
    if op not in THINGY_RECORD_OPS:
        raise _ToolError(f"invalid_input: op must be one of {', '.join(THINGY_RECORD_OPS)}")
    params = {
        key: value
        for key, value in args.items()
        if key in {"collection", "id", "value", "if_version", "limit", "cursor"}
    }
    if op in {"create", "replace", "delete"}:
        params["operation_id"] = _operation_id(args, call_id)
    path = f"/{thingy_id}"
    revision = (await server.call("GET", path)).get("published_revision")
    if not isinstance(revision, str):
        raise _ToolError("This thingy has not been published; publish a revision first")
    request = {"protocol": 1, "method": THINGY_RECORD_OPS[op], "params": params}
    for attempt in range(2):
        try:
            answer = await server.call(
                "POST",
                f"{path}/rpc",
                body={**request, "request_id": uuid.uuid4().hex, "revision_id": revision},
            )
        except _ToolError as exc:
            details = exc.error.get("details") if isinstance(exc.error, dict) else None
            # Someone published meanwhile; the records stay the same, so ask again.
            if attempt == 0 and isinstance(details, dict) and "published_revision" in details:
                revision = details["published_revision"]
                continue
            raise
        result = answer.get("result") if isinstance(answer, dict) else answer
        # Always an object, so a missing record reads the same in every harness.
        return {"record": result} if op == "get" else result
    raise AssertionError("unreachable")


async def _run(
    tool_name: str,
    args: dict[str, Any],
    *,
    server: _Server,
    conversation_id: str,
    call_id: str | None,
) -> Any:
    if tool_name == "sys_thingy_list":
        return await server.call("GET", "")
    if tool_name == "sys_thingy_create":
        created = await server.call("POST", "", body={"name": args.get("name")})
        session_id = args.get("session_id", conversation_id)
        if session_id is None:
            return created
        try:
            await server.call(
                "PUT",
                f"/{created['id']}/sessions/{_segment(session_id, 'session_id')}",
                body={"linked": True},
            )
        except _ToolError as exc:
            # The thingy exists either way; say so rather than lose its ID.
            return {**created, "shown_in_session": None, "show_error": exc.error}
        return {**created, "shown_in_session": session_id}
    thingy_id = _thingy_id(args)
    path = f"/{thingy_id}"
    if tool_name == "sys_thingy_get":
        return await server.call("GET", path)
    if tool_name == "sys_thingy_describe":
        return await server.call("GET", f"{path}/describe")
    if tool_name == "sys_thingy_rename":
        return await server.call("PATCH", path, body={"name": args.get("name")})
    if tool_name == "sys_thingy_action_list":
        query = {"limit": args.get("limit"), "cursor": args.get("cursor")}
        return await server.call("GET", f"{path}/actions", params=query)
    if tool_name == "sys_thingy_action_update":
        changes = {
            key: value for key, value in args.items() if key in {"state", "summary", "session_id"}
        }
        body = {
            "if_version": args.get("if_version"),
            "operation_id": _operation_id(args, call_id),
            **changes,
        }
        action = _segment(args.get("id"), "id")
        return await server.call("PATCH", f"{path}/actions/{action}", body=body)
    if tool_name == "sys_thingy_comment_list":
        return await server.call("GET", f"{path}/comments", params={"status": args.get("status")})
    if tool_name == "sys_thingy_comment_update":
        comment = _segment(args.get("id"), "id")
        return await server.call(
            "PATCH", f"{path}/comments/{comment}", body={"status": args.get("status")}
        )
    if tool_name == "sys_thingy_revision_get":
        revision = args.get("revision_id") or "published"
        return await server.call("GET", f"{path}/revisions/{_segment(revision, 'revision_id')}")
    if tool_name == "sys_thingy_revision_create":
        body = {key: value for key, value in args.items() if key != "thingy_id"}
        return await server.call("POST", f"{path}/revisions", body=body)
    if tool_name == "sys_thingy_publish":
        body = {"revision_id": args.get("revision_id"), "if_version": args.get("if_version")}
        return await server.call("POST", f"{path}/publish", body=body)
    if tool_name == "sys_thingy_records":
        return await _record(server, args, thingy_id, call_id)
    raise _ToolError(f"unknown thingy tool {tool_name}")


async def execute_thingy_tool(
    tool_name: str,
    arguments: str,
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
    call_id: str | None = None,
) -> str:
    """Run one ``sys_thingy_*`` call against the server's thingy routes.

    :param tool_name: One of :data:`THINGY_TOOL_NAMES`.
    :param arguments: The call's JSON arguments.
    :param server_client: HTTP client pointed at the Omnigent server, authenticated
        as the session's owner.
    :param conversation_id: The calling session.
    :param call_id: The harness's tool call ID; a write without an ``operation_id``
        derives one from it, so a retried call is applied once.
    :returns: The route's JSON; ``{"error": ...}`` on failure, with the server's
        ``code``, ``message`` and ``details`` when it refused the call.
    """
    if server_client is None or not conversation_id:
        return json.dumps({"error": f"{tool_name} requires server access"})
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})
    if not isinstance(args, dict):
        return json.dumps({"error": f"{tool_name}: arguments must be an object"})
    try:
        result = await _run(
            tool_name,
            args,
            server=_Server(server_client, conversation_id),
            conversation_id=conversation_id,
            call_id=call_id,
        )
    except _ToolError as exc:
        return json.dumps({"error": exc.error}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)
