"""Central notebook routes; the host supplies its existing authenticated relay.

No Store, upstream address, credential, retry loop or model transport lives here.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
Method = Literal["GET", "PATCH"]
Authorize = Callable[[Request, str], Awaitable[None]]


@dataclass(frozen=True)
class NotebookReply:
    """Bounded, session-tagged relay response. Transport headers stay internal."""

    session_id: str
    status: int
    body: bytes = field(repr=False)


class NotebookTransport(Protocol):
    """Adapt the assigned runner's existing tunnel, not a browser-supplied URL.

    The adapter must limit response bytes while receiving, disable redirects and
    automatic retries, and return the identity of the runner actually selected.
    """

    async def forward_notebook(
        self, session_id: str, method: Method, body: bytes, *, max_response_bytes: int
    ) -> NotebookReply: ...


class NotebookUnavailable(Exception):
    """No assigned Docloop runner; raised before any edit is dispatched."""


def _response(value: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        value,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _error(status: int, code: str, message: str, outcome: str) -> JSONResponse:
    return _response({"error": message, "code": code, "outcome": outcome}, status)


def _snapshot(value: object, session_id: str) -> dict:
    """Validate and positively project the bounded v1 notebook wire format."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Unsupported notebook schema")
    if value.get("session_id") != session_id:
        raise ValueError("Mismatched session")
    for key in ("binding_id", "revision"):
        if not isinstance(value.get(key), str) or not DIGEST.fullmatch(value[key]):
            raise ValueError("Missing notebook identity")
    if value.get("format") not in {"org", "ipynb"}:
        raise ValueError("Unsupported format")
    name = value.get("document_name")
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or any(c in name for c in ("/", "\\", "\x00"))
    ):
        raise ValueError("Expected document basename only")
    entries = value.get("nodes")
    if not isinstance(entries, list) or len(entries) > 4096:
        raise ValueError("Invalid notebook node list")
    nodes, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid notebook node")
        node_id = entry.get("id")
        if not isinstance(node_id, str) or not node_id or len(node_id) > 128 or node_id in seen:
            raise ValueError("Invalid or repeated node identity")
        seen.add(node_id)
        for key in ("source", "output_text", "kind", "language"):
            if not isinstance(entry.get(key), str):
                raise ValueError("Missing node text")
        for key in ("editable", "source_truncated", "output_truncated"):
            if type(entry.get(key)) is not bool:
                raise ValueError("Missing node flags")
        if type(entry.get("source_bytes")) is not int or entry["source_bytes"] < 0:
            raise ValueError("Invalid source size")
        if type(entry.get("level")) is not int:
            raise ValueError("Invalid outline level")
        if entry.get("role") is not None and not isinstance(entry["role"], str):
            raise ValueError("Invalid role")
        nodes.append(
            {
                key: entry[key]
                for key in (
                    "id",
                    "kind",
                    "language",
                    "level",
                    "source",
                    "source_truncated",
                    "source_bytes",
                    "output_text",
                    "output_truncated",
                    "role",
                    "editable",
                )
            }
        )
    caps = value.get("capabilities")
    if not isinstance(caps, dict) or any(
        type(caps.get(k)) is not bool for k in ("edit_source", "create_node", "direct_execution")
    ):
        raise ValueError("Invalid notebook capabilities")
    if caps["direct_execution"]:
        raise ValueError("No new executor through the notebook gateway")
    return {
        "schema_version": 1,
        "session_id": session_id,
        "binding_id": value["binding_id"],
        "revision": value["revision"],
        "format": value["format"],
        "document_name": name,
        "nodes": nodes,
        "capabilities": {k: caps[k] for k in ("edit_source", "create_node", "direct_execution")},
        "execution_hint": "Ask this session's agent to execute cells.",
    }


def notebook_gateway_router(
    authorize: Authorize,
    transport: NotebookTransport,
    *,
    prefix: str = "/v1",
    timeout_seconds: float = 15.0,
    max_request_bytes: int = MAX_REQUEST,
    max_response_bytes: int = MAX_RESPONSE,
) -> APIRouter:
    """Forward only notebook reads/edits after central authorization.

    Any failure after dispatch retains an unknown edit outcome. The browser may
    explicitly retry its original change_id; the gateway never retries a write.
    """
    if not callable(authorize) or not callable(getattr(transport, "forward_notebook", None)):
        raise ValueError("Authorizer and assigned-runner notebook transport are required")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("A finite positive relay timeout is required")
    for limit in (max_request_bytes, max_response_bytes):
        if type(limit) is not int or limit <= 0:
            raise ValueError("Relay byte limits must be positive integers")
    router = APIRouter(prefix=prefix)

    async def handle(request: Request, session_id: str, method: Method):
        if not SESSION.fullmatch(session_id):
            return _error(400, "invalid_session", "Invalid session ID", "not_dispatched")
        # Identity/permissions run before a body read or a runner lookup.
        check = authorize(request, session_id)
        if not inspect.isawaitable(check):
            raise TypeError("Notebook authorization must be asynchronous")
        await check
        if request.url.query:
            return _error(
                400,
                "unsupported_query",
                "Notebook routes accept no query parameters",
                "not_dispatched",
            )
        body = b""
        submitted: dict = {}
        if method == "PATCH":
            if (
                request.headers.get("x-docloop-edit") != "1"
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return _error(
                    403,
                    "edit_origin",
                    "Use the authorized session notebook editor",
                    "not_dispatched",
                )
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                return _error(
                    415, "edit_type", "Notebook edits require application/json", "not_dispatched"
                )
            if request.headers.get("content-encoding", "identity").lower() != "identity":
                return _error(
                    415,
                    "edit_encoding",
                    "Compressed notebook edits are not supported",
                    "not_dispatched",
                )
            length = request.headers.get("content-length")
            if length is not None:
                if not length.isascii() or not length.isdigit() or len(length) > 16:
                    return _error(
                        400, "edit_length", "Invalid notebook edit length", "not_dispatched"
                    )
                if int(length) > max_request_bytes:
                    return _error(
                        413,
                        "edit_size",
                        "Notebook edit exceeds the request limit",
                        "not_dispatched",
                    )
            chunks, size = [], 0
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_request_bytes:
                        return _error(
                            413,
                            "edit_size",
                            "Notebook edit exceeds the request limit",
                            "not_dispatched",
                        )
                    chunks.append(chunk)
            except ClientDisconnect:
                return _error(
                    400,
                    "edit_interrupted",
                    "Notebook edit upload was interrupted",
                    "not_dispatched",
                )
            body = b"".join(chunks)
            if length is not None and int(length) != len(body):
                return _error(
                    400,
                    "edit_length",
                    "Notebook edit length does not match its body",
                    "not_dispatched",
                )
            try:
                submitted = json.loads(body)
                if not isinstance(submitted, dict):
                    raise ValueError("Invalid edit envelope")
                for key in ("revision", "binding_id"):
                    if not isinstance(submitted.get(key), str) or not DIGEST.fullmatch(
                        submitted[key]
                    ):
                        raise ValueError("Invalid edit identity")
                from uuid import UUID

                change = UUID(submitted["change_id"])
                if change.version != 4 or str(change) != submitted["change_id"]:
                    raise ValueError("Invalid edit receipt identity")
            except (ValueError, TypeError, KeyError, UnicodeError, AttributeError, RecursionError):
                return _error(
                    400,
                    "edit_envelope",
                    "Expected a versioned notebook edit and canonical change_id",
                    "not_dispatched",
                )

        outcome = "unknown" if method == "PATCH" else "not_applicable"
        try:
            async with asyncio.timeout(timeout_seconds):
                reply = await transport.forward_notebook(
                    session_id,
                    method,
                    body,
                    max_response_bytes=max_response_bytes,
                )
        except NotebookUnavailable:
            return _error(
                503,
                "notebook_unavailable",
                "No initialized Docloop runner is assigned to this session",
                "not_dispatched",
            )
        except TimeoutError:
            return _error(
                504,
                "notebook_timeout",
                "Runner timed out; retry the same save to confirm its outcome",
                outcome,
            )
        except Exception:  # noqa: BLE001 — transport errors may include private runner details.
            return _error(502, "notebook_transport", "Notebook runner transport failed", outcome)

        try:
            if not isinstance(reply, NotebookReply) or reply.session_id != session_id:
                raise ValueError("Wrong runner identity")
            if type(reply.body) is not bytes or len(reply.body) > max_response_bytes:
                raise ValueError("Oversized notebook reply")
            if type(reply.status) is not int or not 100 <= reply.status <= 599:
                raise ValueError("Invalid reply status")
            payload = json.loads(reply.body)
            if reply.status == 200:
                if method == "GET":
                    result = _snapshot(payload, session_id)
                else:
                    if not isinstance(payload, dict) or type(payload.get("replayed")) is not bool:
                        raise ValueError("Invalid edit receipt")
                    document = _snapshot(payload.get("document"), session_id)
                    if document["binding_id"] != submitted["binding_id"]:
                        raise ValueError("Edit receipt document binding changed")
                    if "change_id" in payload and payload["change_id"] != submitted["change_id"]:
                        raise ValueError("Edit receipt identity changed")
                    result = {"document": document, "replayed": payload["replayed"]}
                response = _response(result)
                if len(response.body) > max_response_bytes:
                    raise ValueError("Projected reply exceeds its bound")
                return response
            if method == "PATCH":
                if (
                    isinstance(payload, dict)
                    and reply.status == 409
                    and payload.get("code") == "revision_conflict"
                ):
                    return _error(
                        409,
                        "revision_conflict",
                        "Document changed; refresh and compare your retained draft",
                        "not_applied",
                    )
                if (
                    isinstance(payload, dict)
                    and reply.status in {400, 403, 404, 413, 415}
                    and payload.get("code") == "edit_rejected"
                    and payload.get("outcome") == "not_applied"
                ):
                    return _error(
                        reply.status,
                        "edit_rejected",
                        "Edit rejected; correct or discard the retained draft",
                        "not_applied",
                    )
                if (
                    isinstance(payload, dict)
                    and reply.status == 503
                    and payload.get("code") == "edit_applied_snapshot_unavailable"
                    and payload.get("outcome") == "applied"
                    and payload.get("change_id") == submitted["change_id"]
                    and payload.get("binding_id") == submitted["binding_id"]
                    and type(payload.get("replayed")) is bool
                ):
                    return _response(
                        {
                            "error": "Edit applied. Retry the same save to refresh its view.",
                            "code": "edit_applied_snapshot_unavailable",
                            "outcome": "applied",
                            "change_id": submitted["change_id"],
                            "binding_id": submitted["binding_id"],
                            "replayed": payload["replayed"],
                        },
                        503,
                    )
                # An older runner can commit, then fail while building its view.
                # Never turn a downstream 4xx into permission to mint a new ID.
                return _error(
                    502,
                    "notebook_edit_unconfirmed",
                    "Save unconfirmed; retry the identical save to check its outcome",
                    "unknown",
                )
            if reply.status in {400, 403, 404, 409, 413, 415, 429, 503, 504}:
                return _error(
                    reply.status,
                    "notebook_unavailable",
                    "Notebook view is unavailable from the assigned runner",
                    "not_applicable",
                )
            return _error(
                502,
                "notebook_upstream",
                "Notebook runner returned an unsupported response",
                "not_applicable",
            )
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return _error(
                502,
                "notebook_invalid_reply",
                "Runner returned an invalid or mismatched notebook response",
                outcome,
            )

    @router.get("/sessions/{session_id}/docloop/document")
    async def get_document(request: Request, session_id: str):
        return await handle(request, session_id, "GET")

    @router.patch("/sessions/{session_id}/docloop/document")
    async def edit_document(request: Request, session_id: str):
        return await handle(request, session_id, "PATCH")

    return router


def register_docloop_forwarding(
    router: APIRouter,
    *,
    enabled: bool = False,
    transport: NotebookTransport | None = None,
    conversation_store=None,
    auth_provider=None,
    permission_store=None,
) -> bool:
    """Opt-in host registration using its existing native permission helpers.

    V1 retains Edit access for reads and writes, matching the prior pane adapter.
    Transport implementation and private app registration remain host-owned.
    """
    if type(enabled) is not bool:
        raise ValueError("enabled must be a bool")
    if not enabled:
        return False
    if transport is None or conversation_store is None:
        raise ValueError("Host conversation store and native notebook transport are required")
    if (auth_provider is None) != (permission_store is None):
        raise ValueError(
            "Configure identity and permissions together, or neither in single-user mode"
        )
    from omnigent.server.auth import LEVEL_EDIT
    from omnigent.server.routes._auth_helpers import require_access_and_level, require_user

    async def authorize(request, session_id):
        user_id = require_user(request, auth_provider)
        await require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )

    router.include_router(notebook_gateway_router(authorize, transport, prefix=""))
    return True
