"""Server-owned Context Saver worker routes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime import get_caps
from omnigent.runtime.context_saver import (
    ContextFile,
    validate_focused_read_worker_model,
)
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.routes._content_type import require_json_content_type

if TYPE_CHECKING:
    from omnigent.stores import ConversationStore
    from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)
_WORKER_UNAVAILABLE_ERROR = "context_saver_worker_unavailable"


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code})


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def register_context_saver_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    runner_tunnel_tokens: frozenset[str] | None = None,
) -> None:
    """Register authenticated inference routes for server-injected workers."""

    async def _authorize(request: Request, session_id: str) -> None:
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        if permission_store is None:
            return
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        if token and runner_tunnel_tokens is not None and token in runner_tunnel_tokens:
            return
        conversation = await asyncio.to_thread(
            conversation_store.get_conversation,
            session_id,
        )
        runner_id = getattr(conversation, "runner_id", None)
        if token and isinstance(runner_id, str) and token_bound_runner_id(token) == runner_id:
            return
        raise OmnigentError(
            "Context Saver inference requires the session's bound runner",
            code=ErrorCode.FORBIDDEN,
        )

    @router.get(
        "/sessions/{session_id}/context-saver/focused-read",
        include_in_schema=False,
    )
    async def focused_read_worker_available(
        request: Request,
        session_id: str,
    ) -> JSONResponse:
        """Report whether this server can perform caller-authenticated inference."""
        await _authorize(request, session_id)
        caps = get_caps()
        if not caps.context_saver_available:
            return _error(403, "context_saver_unavailable")
        return JSONResponse(content={"available": caps.context_saver_worker is not None})

    @router.post(
        "/sessions/{session_id}/context-saver/focused-read",
        include_in_schema=False,
        dependencies=[Depends(require_json_content_type)],
    )
    async def focused_read(
        request: Request,
        session_id: str,
    ) -> JSONResponse:
        """Run Focused Read with the session caller's server-owned worker."""
        await _authorize(request, session_id)
        caps = get_caps()
        if not caps.context_saver_available:
            return _error(403, "context_saver_unavailable")
        worker = caps.context_saver_worker
        if worker is None:
            return _error(409, _WORKER_UNAVAILABLE_ERROR)

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error(400, "invalid_request")
        if not isinstance(payload, dict):
            return _error(400, "invalid_request")

        question = payload.get("question")
        model = payload.get("model")
        allow_source_upload = payload.get("allow_source_upload")
        if (
            not isinstance(question, str)
            or not question.strip()
            or not isinstance(allow_source_upload, bool)
        ):
            return _error(400, "invalid_request")
        try:
            validated_model = validate_focused_read_worker_model(
                model,
                allow_source_upload=allow_source_upload,
            )
        except ValueError:
            return _error(400, "worker_destination_not_approved")

        timeout_seconds = _positive_int(payload.get("timeout_seconds"))
        max_excerpt_lines = _positive_int(payload.get("max_excerpt_lines"))
        output_budget = _positive_int(payload.get("output_budget"))
        limits = caps.context_saver.focused_read
        if (
            timeout_seconds is None
            or timeout_seconds > limits.request_timeout_seconds
            or max_excerpt_lines is None
            or max_excerpt_lines > limits.max_excerpt_lines
            or output_budget is None
            or output_budget > max(256, limits.max_excerpt_lines * 20)
        ):
            return _error(400, "worker_limits_exceeded")

        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files or len(raw_files) > limits.max_files:
            return _error(400, "worker_limits_exceeded")
        files: list[ContextFile] = []
        total_bytes = 0
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                return _error(400, "invalid_request")
            path = raw_file.get("path")
            content = raw_file.get("content")
            if not isinstance(path, str) or not path or not isinstance(content, str):
                return _error(400, "invalid_request")
            file_bytes = len(content.encode("utf-8"))
            total_bytes += file_bytes
            if total_bytes > limits.max_total_bytes:
                return _error(400, "worker_limits_exceeded")
            files.append(
                ContextFile(
                    path=path,
                    content=content,
                    total_lines=len(content.splitlines()),
                    total_bytes=file_bytes,
                )
            )

        try:
            result = await worker.focus(
                files=files,
                question=question,
                model=validated_model,
                allow_source_upload=allow_source_upload,
                timeout_seconds=timeout_seconds,
                max_excerpt_lines=max_excerpt_lines,
                output_budget=output_budget,
            )
        except Exception:
            # Worker exceptions can contain provider payloads; keep them out of
            # both the response and logs.
            _logger.warning(
                "Context Saver server worker failed",
                extra={"session_id": session_id},
            )
            return _error(502, "context_saver_worker_failed")
        return JSONResponse(
            content={
                "content": result.content,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "model": result.reported_model,
            }
        )
