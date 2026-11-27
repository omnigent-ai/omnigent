"""The ``/v1/gadgets`` routes: gadgets, their revisions, records, comments and actions.

Every route answers 404 before reading its body unless the server enables the
``gadgets`` release feature and has a gadget store. Each request builds a
:class:`~omnigent.gadgets.service.GadgetService` for the caller, who in
single-user mode is the local user. Agents reach these routes through the
runner's ``sys_gadget_*`` tools as the session's owner; an agent's call, read or
write, from a session shared with edit access is refused, as for memory.

Errors use the gadget error shape the web app and the sandboxed frame read:
``{"error": {"code", "message", "request_id", "details"?}}`` with codes such as
``NOT_FOUND``, ``CONFLICT``, ``CONSTRAINT_VIOLATION`` and ``PERMISSION_DENIED``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse, Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.gadgets.access import GadgetAccess, workspace_registration
from omnigent.gadgets.actions import ACTION_STATES
from omnigent.gadgets.constraints import ConstraintViolation
from omnigent.gadgets.progress import SessionStateReader
from omnigent.gadgets.service import GadgetError, GadgetService, GadgetViewer, canonical_bytes
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes.gadget_permissions import register_gadget_permissions_routes
from omnigent.server.routes.scheduled_task_permissions import display_names_or_ids
from omnigent.server.session_orchestration import (
    require_named_source_private,
    require_owned_session,
)
from omnigent.stores import ConversationStore
from omnigent.stores.gadget_store import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    GadgetStore,
    InvalidInput,
    NotFound,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

MAX_HTTP_BODY_BYTES = 2 * 1024 * 1024
MAX_RPC_BYTES = 64 * 1024
# ``GET /gadgets/{id}/revisions/published``: revision IDs are hex, so this never names one.
PUBLISHED_REVISION = "published"
_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _json_body(properties: dict[str, Any], required: Sequence[str]) -> dict[str, Any]:
    """OpenAPI for a route that reads its JSON body itself."""
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": properties,
                        "required": list(required),
                    }
                }
            },
        }
    }


_ACTION_ENTRY_RESPONSE = {"description": "The ledger entry: `id`, `value`, `version` and times."}
_STRING = {"type": "string"}
# The gadget codes for the server's general error codes, so every /v1/gadgets route,
# sharing included, answers in one vocabulary.
_OMNIGENT_CODES = {
    ErrorCode.FORBIDDEN: "PERMISSION_DENIED",
    ErrorCode.INVALID_INPUT: "INVALID_ARGUMENT",
    ErrorCode.UNAUTHORIZED: "UNAUTHENTICATED",
    ErrorCode.NOT_FOUND: "NOT_FOUND",
    ErrorCode.CONFLICT: "CONFLICT",
    ErrorCode.ALREADY_EXISTS: "CONFLICT",
}
_GADGET_ERRORS = (
    GadgetError,
    NotFound,
    Conflict,
    InvalidInput,
    CapacityExceeded,
    CorruptData,
    RequestValidationError,
)


def gadgets_enabled(state: Any) -> bool:
    """Whether the server's ``gadgets`` release feature is on."""
    return getattr(state, "feature_flags", FeatureFlags()).enabled(Feature.GADGETS)


def _validation_message(error: RequestValidationError) -> tuple[str, dict]:
    """A readable message for a request body that fails its schema, and the details."""
    problems = []
    for item in error.errors():
        where = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
        problems.append({"field": where or None, "message": str(item.get("msg", "Invalid"))})
    if not problems:
        return "The request body is invalid", {}
    first = problems[0]
    message = f"{first['field']}: {first['message']}" if first["field"] else first["message"]
    return f"Invalid request: {message}", {"errors": problems}


def gadget_failure(error: Exception, request_id: str | None = None) -> JSONResponse:
    """The gadget error response for *error*."""
    details = getattr(error, "details", None)
    details = dict(details) if isinstance(details, dict) else None
    headers = dict(_NO_STORE)
    if isinstance(error, GadgetError):
        code, message, status = error.code, str(error), error.status
    elif isinstance(error, OmnigentError):
        code = _OMNIGENT_CODES.get(str(error.code), str(error.code).upper())
        message, status = error.message, error.http_status
    elif isinstance(error, ConstraintViolation):
        code, message, status = "CONSTRAINT_VIOLATION", str(error), 409
    elif isinstance(error, NotFound):
        code, message, status = "NOT_FOUND", str(error), 404
    elif isinstance(error, Conflict) and error.reason == "busy":
        # Other writers held the gadget's rule guard; the write changed nothing and is safe
        # to retry with the same operation ID.
        code, message, status = "UNAVAILABLE", str(error), 503
        details = {**(details or {}), "reason": "busy", "retryable": True}
        headers["Retry-After"] = "1"
    elif isinstance(error, Conflict):
        code, message, status = "CONFLICT", str(error), 409
        details = details or {"reason": error.reason, "current_version": error.current_version}
    elif isinstance(error, RequestValidationError):
        message, validation = _validation_message(error)
        code, status = "INVALID_ARGUMENT", 400
        details = {**(details or {}), **validation} or None
    elif isinstance(error, InvalidInput):
        code, message, status = "INVALID_ARGUMENT", str(error), 400
    elif isinstance(error, CapacityExceeded):
        code, message, status = "RESOURCE_EXHAUSTED", str(error), 413
    elif isinstance(error, CorruptData):
        code, message, status = (
            "DATA_LOSS",
            "Gadget storage is inconsistent; ask the gadget's manager to check it",
            409,
        )
    else:
        code, message, status = "INVALID_ARGUMENT", "The request is invalid", 400
    payload: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details:
        payload["details"] = details
    return JSONResponse({"error": payload}, status_code=status, headers=headers)


async def _body(request: Request, limit: int = MAX_HTTP_BODY_BYTES) -> dict:
    """The request's JSON object, read up to *limit* bytes."""
    require_json_content_type(request)
    require_trusted_origin(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise CapacityExceeded("Request exceeds its size limit")
        body.extend(chunk)
    try:
        value = json.loads(body)
        canonical_bytes(value)
    except (ValueError, RecursionError) as exc:
        raise InvalidInput("Expected valid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidInput("Expected a JSON object")
    return value


class _GadgetRoute(APIRoute):
    """Gates a route on the feature and answers gadget errors in the gadget shape."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if not gadgets_enabled(request.app.state):
                return gadget_failure(GadgetError("NOT_FOUND", "Not found", 404))
            try:
                response = await original(request)
            except _GADGET_ERRORS as exc:
                return gadget_failure(exc, getattr(request.state, "gadget_request_id", None))
            except OmnigentError as exc:
                if exc.http_status >= 500:
                    raise  # The server's own handler logs those.
                return gadget_failure(exc, getattr(request.state, "gadget_request_id", None))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
                return gadget_failure(
                    GadgetError(
                        "PERMISSION_DENIED" if exc.status_code == 403 else "INVALID_ARGUMENT",
                        detail or "Invalid request content or origin",
                        exc.status_code,
                    )
                )
            response.headers.update(_NO_STORE)
            return response

        return guarded


def viewer_can_read_session(
    user_id: str,
    session_id: str,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> bool:
    """Whether the viewer may show a gadget beside a session, or make it the gadget session.

    The permission store decides: a deployment's session IDs need not be UUIDs, and an ID its
    stores can't parse is one the viewer can't read. Without one (single-user mode) any
    well-formed ID is taken.
    """
    if permission_store is None:
        try:
            uuid_to_bytes(session_id)
        except InvalidUuidError:
            return False
        return True
    try:
        return check_session_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
    except (OmnigentError, ValueError):
        return False


def _limit(request: Request, default: int) -> int:
    """The ``limit`` query parameter as an int; the service checks its range."""
    raw = request.query_params.get("limit", str(default))
    # ASCII digits only, and short: Python refuses to convert very long digit strings.
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 9):
        raise InvalidInput("limit must be an integer")
    return int(raw)


def create_gadgets_router(
    gadget_store: GadgetStore,
    gadget_access: GadgetAccess,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the gadgets router, mounted under ``/v1``.

    :param gadget_store: Where gadgets are kept.
    :param gadget_access: Decides each caller's permissions on a gadget.
    :param conversation_store: Reads the sessions doing a gadget's actions.
    :param auth_provider: Identifies the caller; ``None`` in single-user mode.
    :param permission_store: Checks which sessions the caller may read, refuses an
        agent's call from a shared session, and names grantees; ``None`` skips those.
    """
    router = APIRouter(route_class=_GadgetRoute)

    def _viewer(user_id: str) -> GadgetViewer:
        # Gadgets belong to the account's registration, which a password change keeps. A
        # database read, so it runs in the request's worker thread.
        # Scoped to the request's workspace, so the same user ID in another workspace is
        # someone else.
        raw = (
            permission_store.account_registrations([user_id]).get(user_id)
            if permission_store is not None
            else None
        )
        if (
            raw is None
            and permission_store is not None
            and (
                user_id in permission_store.outlived_accounts([user_id])
                or user_id in permission_store.deleted_accounts([user_id])
            )
        ):
            # The account this request authenticated as is gone (deleted, or its name
            # registered again since). Its writes would land where no cleanup looks, or
            # under the new account: refused, before the workspace names a fallback. A live
            # account without a registration (activated from a sharing placeholder) is
            # still itself, and goes on with none, like an SSO identity.
            raise OmnigentError(
                "This account was deleted; sign in again", code=ErrorCode.UNAUTHORIZED
            )
        registration = workspace_registration(raw)
        return GadgetViewer(user_id, registration)

    def _sessions(user_id: str) -> SessionStateReader:
        def can_read(session_id: str) -> bool:
            return viewer_can_read_session(
                user_id,
                session_id,
                None if auth_provider is None else permission_store,
                conversation_store,
            )

        def can_take_actions(session_id: str) -> bool:
            # The same test an agent's gadget call from that session must pass.
            if auth_provider is None or permission_store is None or user_id == RESERVED_USER_LOCAL:
                return True
            try:
                require_owned_session(
                    session_id, user_id, conversation_store, permission_store, private=True
                )
            except (OmnigentError, ValueError):
                return False
            return True

        owned_by = None if auth_provider is None else user_id
        return SessionStateReader(conversation_store, can_read, owned_by, can_take_actions)

    def _display_names(user_ids: Sequence[str]) -> dict[str, str]:
        if permission_store is None:
            return {user_id: user_id for user_id in user_ids}
        return display_names_or_ids(permission_store, user_ids)

    async def run(request: Request, operation: Callable[[GadgetService], Any]) -> Any:
        user_id = require_user(request, auth_provider) or RESERVED_USER_LOCAL

        def call() -> Any:
            viewer = _viewer(user_id)
            # An agent's call, read or write, must come from the caller's own private
            # session: a collaborator on a shared session must not read the owner's
            # gadgets through the owner's agent, as with memory.
            require_named_source_private(
                request,
                None if auth_provider is None else viewer.user_id,
                conversation_store,
                permission_store,
            )
            service = GadgetService(
                gadget_store,
                gadget_access,
                viewer,
                _sessions(viewer.user_id),
                _display_names,
            )
            return operation(service)

        return await asyncio.to_thread(call)

    def _json(value: Any, status_code: int = 200) -> JSONResponse:
        return JSONResponse(value, status_code=status_code, headers=_NO_STORE)

    def _page(request: Request, default: int) -> tuple[int, str | None]:
        return _limit(request, default), request.query_params.get("cursor")

    _PAGE_PARAMETERS = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100}},
        {"name": "cursor", "in": "query", "schema": _STRING},
    ]

    @router.get("/gadgets", openapi_extra={"parameters": _PAGE_PARAMETERS})
    async def list_gadgets(request: Request) -> Response:
        """A page of the caller's own gadgets, in the store's order: ``{"items", "next_cursor"}``.

        Follow ``next_cursor``; a page can be short and still continue.
        """
        limit, cursor = _page(request, 100)
        return _json(await run(request, lambda service: service.list_owned(limit, cursor)))

    @router.post(
        "/gadgets", status_code=201, openapi_extra=_json_body({"name": _STRING}, ["name"])
    )
    async def create_gadget(request: Request) -> Response:
        """Create a private gadget from ``{"name"}``; answers the gadget with 201."""
        body = await _body(request)
        result = await run(request, lambda service: service.create(body.get("name")))
        return _json(result, 201)

    # Before ``/gadgets/{app_id}``, which would otherwise match these.
    @router.get("/gadgets/shared", openapi_extra={"parameters": _PAGE_PARAMETERS})
    async def shared_gadgets(request: Request) -> Response:
        """A page of other people's gadgets shared with the caller, ``{"items",
        "next_cursor"}``, each with ``shared_via`` (``"user"`` or ``"group"``) and
        ``owner_email`` (the owner's display name). A page can be short and continue."""
        limit, cursor = _page(request, 100)
        return _json(await run(request, lambda service: service.shared(limit, cursor)))

    _SHELF_PARAMETERS = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
        {"name": "cursor", "in": "query", "schema": _STRING},
    ]

    @router.get("/gadgets/shelf", openapi_extra={"parameters": _SHELF_PARAMETERS})
    async def shelf(request: Request) -> Response:
        """A page of the caller's pins, ``{"pinned": [{"id", "name"}], "has_owned",
        "next_cursor"}``, for the sidebar."""
        limit, cursor = _page(request, 50)
        return _json(await run(request, lambda service: service.shelf(limit, cursor)))

    _SESSION_GADGETS_PARAMETERS = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}},
        {"name": "cursor", "in": "query", "schema": _STRING},
    ]

    @router.get(
        "/gadgets/sessions/{session_id}", openapi_extra={"parameters": _SESSION_GADGETS_PARAMETERS}
    )
    async def session_gadgets(session_id: str, request: Request) -> Response:
        """A page of the gadgets shown beside one of the caller's sessions, ``{"session_id",
        "items", "next_cursor"}``."""
        limit, cursor = _page(request, 20)
        return _json(
            await run(request, lambda service: service.session_gadgets(session_id, limit, cursor))
        )

    @router.put("/gadgets/{app_id}/pin")
    async def pin(app_id: str, request: Request) -> Response:
        """Pin or unpin a readable gadget with ``{"pinned": bool}``; answers the shelf."""
        body = await _body(request)
        return _json(await run(request, lambda service: service.pin(app_id, body)))

    @router.put("/gadgets/{app_id}/sessions/{session_id}")
    async def link_session(app_id: str, session_id: str, request: Request) -> Response:
        """Show a gadget beside a session (``{"linked": bool}``); answers its gadgets.

        Showing it there also sends the caller's actions from the gadget's page to that
        session, unless the body has ``"handles_actions": false``.
        """
        body = await _body(request)
        return _json(
            await run(request, lambda service: service.link_session(app_id, session_id, body))
        )

    @router.get("/gadgets/{app_id}")
    async def get_gadget(app_id: str, request: Request) -> Response:
        """One gadget with the caller's ``permissions``."""
        return _json(await run(request, lambda service: service.get(app_id)))

    @router.patch("/gadgets/{app_id}")
    async def rename_gadget(app_id: str, request: Request) -> Response:
        """Rename a gadget for everyone with ``{"name"}``; needs publish permission."""
        body = await _body(request)
        return _json(await run(request, lambda service: service.rename(app_id, body)))

    @router.delete(
        "/gadgets/{app_id}",
        openapi_extra={
            "parameters": [{"name": "if_unbuilt", "in": "query", "schema": {"type": "boolean"}}]
        },
    )
    async def delete_gadget(app_id: str, request: Request) -> Response:
        """Delete a gadget for everyone; needs manage permission. With ``?if_unbuilt=true``
        only while it was never published nor taken by the caller's gadget session:
        ``{"deleted": false}`` when it was kept."""
        require_trusted_origin(request)
        if request.query_params.get("if_unbuilt") == "true":
            return _json(await run(request, lambda service: service.discard_unbuilt(app_id)))
        return _json(await run(request, lambda service: service.delete(app_id)))

    @router.get("/gadgets/{app_id}/session/status")
    async def gadget_session_status(app_id: str, request: Request) -> Response:
        """Whether the caller's gadget session and the sessions it started are working."""
        return _json(await run(request, lambda service: service.gadget_session_status(app_id)))

    @router.post("/gadgets/{app_id}/revisions", status_code=201)
    async def create_revision(app_id: str, request: Request) -> Response:
        """Upload an immutable revision; answers ``{"id"}`` with 201."""
        body = await _body(request)
        result = await run(request, lambda service: service.create_revision(app_id, body))
        return _json(result, 201)

    @router.get("/gadgets/{app_id}/revisions/{revision_id}")
    async def get_revision(app_id: str, revision_id: str, request: Request) -> Response:
        """A revision's HTML and configuration; ``published`` names the published one."""
        wanted = None if revision_id == PUBLISHED_REVISION else revision_id
        return _json(await run(request, lambda service: service.get_revision(app_id, wanted)))

    @router.get("/gadgets/{app_id}/describe")
    async def describe(app_id: str, request: Request) -> Response:
        """What an agent reads before working with the records: the published revision's
        guide, collection schemas, constraints and actions, without its HTML."""
        return _json(await run(request, lambda service: service.describe(app_id)))

    @router.post(
        "/gadgets/{app_id}/publish",
        openapi_extra=_json_body(
            {"revision_id": _STRING, "if_version": {"type": ["string", "null"]}}, ["revision_id"]
        ),
    )
    async def publish(app_id: str, request: Request) -> Response:
        """Publish ``{"revision_id", "if_version"}``; answers the gadget."""
        body = await _body(request)
        return _json(await run(request, lambda service: service.publish(app_id, body)))

    @router.get(
        "/gadgets/{app_id}/comments",
        openapi_extra={
            "parameters": [
                {
                    "name": "status",
                    "in": "query",
                    "schema": {"type": "string", "enum": ["draft", "addressed"]},
                },
                *_PAGE_PARAMETERS,
            ]
        },
    )
    async def list_comments(app_id: str, request: Request) -> Response:
        """A page of comments in the order they were made,
        ``{"items", "next_cursor", "can_comment"}``, optionally only ``?status=``; a page
        can be short and still continue."""
        status = request.query_params.get("status")
        limit, cursor = _page(request, 100)
        return _json(
            await run(
                request, lambda service: service.list_comments(app_id, status, limit, cursor)
            )
        )

    @router.post("/gadgets/{app_id}/comments", status_code=201)
    async def create_comment(app_id: str, request: Request) -> Response:
        """Add ``{"body", "anchor", "operation_id"}``; answers the comment with 201."""
        body = await _body(request)
        result = await run(request, lambda service: service.create_comment(app_id, body))
        return _json(result, 201)

    @router.patch("/gadgets/{app_id}/comments/{comment_id}")
    async def update_comment(app_id: str, comment_id: str, request: Request) -> Response:
        """Change a comment's ``body`` (its author) or ``status``."""
        body = await _body(request)
        return _json(
            await run(request, lambda service: service.update_comment(app_id, comment_id, body))
        )

    @router.delete("/gadgets/{app_id}/comments/{comment_id}")
    async def delete_comment(app_id: str, comment_id: str, request: Request) -> Response:
        """Delete a comment: its author, or someone who manages the gadget."""
        require_trusted_origin(request)
        return _json(
            await run(request, lambda service: service.delete_comment(app_id, comment_id))
        )

    @router.get("/gadgets/{app_id}/session")
    async def get_gadget_session(app_id: str, request: Request) -> Response:
        """The caller's gadget session: ``{"session_id", "version"}``."""
        return _json(await run(request, lambda service: service.get_gadget_session(app_id)))

    @router.put(
        "/gadgets/{app_id}/session",
        openapi_extra=_json_body({"session_id": _STRING}, ["session_id"]),
    )
    async def set_gadget_session(app_id: str, request: Request) -> Response:
        """Register ``{"session_id"}`` as the caller's gadget session."""
        body = await _body(request)
        return _json(await run(request, lambda service: service.set_gadget_session(app_id, body)))

    @router.get(
        "/gadgets/{app_id}/actions",
        openapi_extra={
            "parameters": [
                {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                {"name": "cursor", "in": "query", "schema": _STRING},
            ]
        },
    )
    async def list_actions(app_id: str, request: Request) -> Response:
        """A page of the action ledger: ``{"items", "next_cursor"}``."""
        limit = _limit(request, 50)
        cursor = request.query_params.get("cursor")
        return _json(
            await run(request, lambda service: service.list_actions(app_id, limit, cursor))
        )

    @router.post(
        "/gadgets/{app_id}/actions",
        status_code=201,
        responses={
            201: _ACTION_ENTRY_RESPONSE,
            200: {"description": "Joined an open request for the same action (`merged`)."},
        },
        openapi_extra=_json_body(
            {"action": _STRING, "params": {"type": "object"}, "operation_id": _STRING},
            ["action", "operation_id"],
        ),
    )
    async def request_action(app_id: str, request: Request) -> Response:
        """Queue ``{"action", "params", "operation_id"}``; 201, or 200 when it joined an
        open request for the same action (``"merged": true``)."""
        body = await _body(request, MAX_RPC_BYTES)
        result = await run(request, lambda service: service.request_action(app_id, body))
        return _json(result, 200 if result.get("merged") else 201)

    # Before ``/gadgets/{app_id}/actions/{action_id}/...``.
    @router.get("/gadgets/{app_id}/actions/progress")
    async def action_progress(app_id: str, request: Request) -> Response:
        """Progress of ``?ids=a,b``, or of the caller's open actions without ids."""
        ids = [value for value in request.query_params.get("ids", "").split(",") if value]
        return _json(await run(request, lambda service: service.action_progress(app_id, ids)))

    @router.patch(
        "/gadgets/{app_id}/actions/{action_id}",
        responses={200: _ACTION_ENTRY_RESPONSE},
        openapi_extra=_json_body(
            {
                "if_version": _STRING,
                "operation_id": _STRING,
                "state": {"type": "string", "enum": list(ACTION_STATES)},
                # ``null`` clears either.
                "summary": {"type": ["string", "null"]},
                "session_id": {"type": ["string", "null"]},
            },
            ["if_version", "operation_id"],
        ),
    )
    async def update_action(app_id: str, action_id: str, request: Request) -> Response:
        """Record ``state``, ``summary`` or ``session_id`` with ``if_version`` and
        ``operation_id``."""
        body = await _body(request, MAX_RPC_BYTES)
        return _json(
            await run(
                request, lambda service: service.update_action(app_id, {**body, "id": action_id})
            )
        )

    @router.post(
        "/gadgets/{app_id}/actions/{action_id}/delivery",
        openapi_extra=_json_body({"session_id": _STRING}, ["session_id"]),
    )
    async def record_delivery(app_id: str, action_id: str, request: Request) -> Response:
        """Record that the web app posted the action into ``{"session_id"}``."""
        body = await _body(request, MAX_RPC_BYTES)
        return _json(
            await run(request, lambda service: service.record_delivery(app_id, action_id, body))
        )

    @router.get("/gadgets/{app_id}/content")
    async def content(app_id: str, request: Request) -> Response:
        """The published revision's HTML and the capabilities granted to the caller."""
        return _json(await run(request, lambda service: service.content(app_id)))

    @router.post("/gadgets/{app_id}/rpc")
    async def rpc(app_id: str, request: Request) -> Response:
        """The sandboxed frame's data bridge: ``{"protocol": 1, "request_id",
        "revision_id", "method", "params"}`` answers ``{"result"}``."""
        body = await _body(request, MAX_RPC_BYTES)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise InvalidInput("Invalid request ID")
        request.state.gadget_request_id = request_id
        if type(body.get("protocol")) is not int or body["protocol"] != 1:
            raise GadgetError("UNSUPPORTED", "Unsupported gadget protocol", 400)
        method = body.get("method")
        if not isinstance(method, str):
            raise InvalidInput("RPC method must be a string")
        params = body.get("params", {})
        result = await run(
            request, lambda service: service.rpc(app_id, body.get("revision_id"), method, params)
        )
        encoded = canonical_bytes({"result": result})
        if len(encoded) > MAX_RPC_BYTES:
            raise CapacityExceeded("RPC response exceeds its message limit")
        return Response(encoded, media_type="application/json", headers=_NO_STORE)

    register_gadget_permissions_routes(
        router,
        gadget_store=gadget_store,
        gadget_access=gadget_access,
        auth_provider=auth_provider,
        permission_store=permission_store,
    )
    return router
