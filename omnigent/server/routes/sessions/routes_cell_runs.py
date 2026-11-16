"""Code cell runs: execute a user-clicked Python block in the session workspace.

A run is a ``sys_os_shell`` call started by the user. It goes through the same
MCP proxy path as the agent's own shell calls: TOOL_CALL policy (with a
server-held approval card on ASK), the runner's granted-tool check, the
session's sandbox and workspace, and TOOL_RESULT policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError, WrongReplicaError
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes._auth_helpers import require_access as _require_access
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._sessions.common import _logger
from omnigent.server.routes._sessions.helpers import (
    _build_actor,
    _get_runner_client_for_resource_access,
    _runner_lookup_error_must_surface,
)
from omnigent.server.routes._sessions.orchestration import (
    MCP_TOOL_CALL_DECLINED_MESSAGE,
    MCP_TOOL_CALL_NOT_APPROVED_MESSAGE,
    MCP_TOOL_CALL_TOO_LONG_TO_REVIEW_MESSAGE,
    _handle_mcp_tools_call,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore

# UTF-8 bytes; the command must stay under Linux's 128 KiB single-argument limit.
CELL_CODE_MAX_BYTES = 64 * 1024
CELL_TIMEOUT_DEFAULT_S = 60
CELL_TIMEOUT_MAX_S = 300
# Per stream; the shell helper already stops at 100,000 characters.
CELL_OUTPUT_MAX_CHARS = 50_000
_CELL_RUN_TOOL = "sys_os_shell"
_HEREDOC_PREFIX = "OMNIGENT_CELL_"


class CellRunRequest(BaseModel):
    """Body of ``POST /v1/sessions/{session_id}/cell-runs``."""

    model_config = ConfigDict(extra="forbid")

    language: Literal["python"] = Field(description="Cell language; only Python is supported.")
    code: str = Field(
        min_length=1, max_length=CELL_CODE_MAX_BYTES, description="Cell source, up to 64 KiB."
    )
    timeout_s: int = Field(
        default=CELL_TIMEOUT_DEFAULT_S,
        ge=1,
        le=CELL_TIMEOUT_MAX_S,
        description="Seconds before the run is stopped.",
    )


class CellRunResponse(BaseModel):
    """Outcome of one cell run."""

    status: Literal["completed", "timed_out", "failed"] = Field(
        description=(
            "``completed`` when the interpreter exited (any exit code), ``timed_out`` when "
            "the timeout stopped it, ``failed`` when it could not run or its result was withheld."
        )
    )
    exit_code: int | None = Field(description="Interpreter exit code, when it exited.")
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    error: str | None = Field(default=None, description="Why a failed run produced no result.")


def _heredoc_delimiter(code: str) -> str:
    return _HEREDOC_PREFIX + hashlib.sha256(code.encode()).hexdigest()[:16]


def python_cell_command(code: str) -> str:
    """
    Build the shell command that runs *code* with ``python3`` from stdin.

    The code travels in a quoted heredoc, so the shell expands nothing in it.
    The delimiter is derived from the code, so a retried call is identical.

    :param code: Python source, e.g. ``"print(1)"``.
    :returns: A POSIX shell command, e.g.
        ``"python3 - <<'OMNIGENT_CELL_…'\\nprint(1)\\nOMNIGENT_CELL_…\\n"``.
    :raises ValueError: If the code holds a NUL or a line equal to the delimiter.
    """
    if "\x00" in code:
        raise ValueError("Code must not contain NUL characters")
    delimiter = _heredoc_delimiter(code)
    # The shell ends a heredoc at the first line equal to its delimiter.
    if delimiter in code.split("\n"):
        raise ValueError("Code contains a reserved line")
    body = code if code.endswith("\n") else code + "\n"
    return f"python3 - <<'{delimiter}'\n{body}{delimiter}\n"


def _capped(text: object) -> tuple[str, bool]:
    if not isinstance(text, str):
        return "", False
    if len(text) <= CELL_OUTPUT_MAX_CHARS:
        return text, False
    return text[:CELL_OUTPUT_MAX_CHARS], True


def cell_run_result(output: str, *, duration_ms: int) -> CellRunResponse:
    """
    Shape a ``sys_os_shell`` result into a :class:`CellRunResponse`.

    :param output: The tool's result text: the shell helper's JSON, or a
        policy notice when TOOL_RESULT policy withheld it.
    :param duration_ms: Wall time of the call, e.g. ``412``.
    :returns: The run outcome with each stream capped.
    """
    try:
        data: Any = json.loads(output)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        error, _ = _capped(output or "The run returned no result")
        return CellRunResponse(
            status="failed",
            exit_code=None,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=duration_ms,
            error=error,
        )
    stdout, stdout_truncated = _capped(data.get("stdout"))
    stderr, stderr_truncated = _capped(data.get("stderr"))
    exit_code = data.get("exit_code")
    if data.get("timed_out") is True:
        status: Literal["completed", "timed_out", "failed"] = "timed_out"
        error = None
    elif isinstance(exit_code, int) and not isinstance(exit_code, bool):
        # A nonzero exit is a normal outcome; stderr carries the traceback.
        status, error = "completed", None
    else:
        status = "failed"
        raw_error = data.get("error")
        error = raw_error if isinstance(raw_error, str) else "The run returned no result"
    return CellRunResponse(
        status=status,
        exit_code=exit_code if status == "completed" else None,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_ms=duration_ms,
        error=error,
    )


def _refusal_status(message: str) -> int:
    if message.startswith("Denied by policy") or message in (
        MCP_TOOL_CALL_DECLINED_MESSAGE,
        MCP_TOOL_CALL_NOT_APPROVED_MESSAGE,
        MCP_TOOL_CALL_TOO_LONG_TO_REVIEW_MESSAGE,
    ):
        return 403
    if message.startswith("No runner bound"):
        return 409
    return 502


async def _require_runner_on_this_replica(session_id: str, conversation: Conversation) -> None:
    """
    Refuse a run whose runner this replica can't reach, before any policy work.

    A runner on another replica raises the typed wrong-replica error the
    client re-addresses on; nothing has been asked or run at that point.

    :param session_id: The session to run in, e.g. ``"conv_abc123"``.
    :param conversation: The session's already-loaded conversation row.
    :raises WrongReplicaError: When the runner is live on another replica.
    :raises HTTPException: 409 when the session has no reachable runner.
    """
    try:
        runner = await _get_runner_client_for_resource_access(
            session_id, conversation=conversation
        )
    except (LookupError, httpx.HTTPError, OmnigentError) as exc:
        if isinstance(exc, WrongReplicaError) or _runner_lookup_error_must_surface(exc):
            raise
        runner = None
    if runner is None:
        raise HTTPException(status_code=409, detail=f"No runner bound for session {session_id!r}")


def register_cell_run_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Register ``POST /sessions/{session_id}/cell-runs`` on *router*."""

    @router.post(
        "/sessions/{session_id}/cell-runs",
        response_model=CellRunResponse,
        # A JSON body forces a CORS preflight, so other sites can't start runs.
        dependencies=[Depends(require_json_content_type)],
    )
    async def run_session_cell(
        session_id: str,
        body: CellRunRequest,
        request: Request,
    ) -> CellRunResponse:
        """
        Run one Python cell in the session's workspace and return its output.

        Requires edit access and the ``code_cell_runs`` feature. The run is the
        session agent's ``sys_os_shell`` tool started by the caller: the
        agent must have that tool, its policies apply, and an approval they
        ask for is shown in the session and awaited before anything runs.
        """
        flags = getattr(request.app.state, "feature_flags", FeatureFlags())
        if not flags.enabled(Feature.CODE_CELL_RUNS):
            raise HTTPException(status_code=404, detail="Code cell runs are disabled")
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if conv.archived:
            raise HTTPException(status_code=409, detail="Archived sessions can't run code")
        if len(body.code.encode()) > CELL_CODE_MAX_BYTES:
            raise HTTPException(status_code=400, detail="Code is longer than 64 KiB")
        try:
            command = python_cell_command(body.code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The MCP handler resolves the runner again at dispatch, after any approval.
        await _require_runner_on_this_replica(session_id, conv)

        started = time.monotonic()
        rpc_response = await _handle_mcp_tools_call(
            1,
            session_id,
            {"name": _CELL_RUN_TOOL, "arguments": {"command": command, "timeout": body.timeout_s}},
            conversation_store,
            agent_store,
            runner_router,
            # The clicking user is the actor, never the agent's turn actor.
            actor=_build_actor(user_id),
            credential_user=user_id,
            request=request,
            hold_ask_on_server=True,
            hold_ask_label=f"Python cell run by {user_id or 'a user'}",
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        payload = json.loads(bytes(rpc_response.body))
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "The run failed")
            status_code = _refusal_status(message)
            _logger.info(
                "Code cell run refused: session=%s status=%d code_chars=%d",
                session_id,
                status_code,
                len(body.code),
                extra={"session_id": session_id},
            )
            raise HTTPException(status_code=status_code, detail=message)
        content = (payload.get("result") or {}).get("content") or []
        text = next(
            (
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ),
            "",
        )
        result = cell_run_result(text, duration_ms=duration_ms)
        _logger.info(
            "Code cell run finished: session=%s status=%s exit_code=%s code_chars=%d "
            "duration_ms=%d truncated=%s",
            session_id,
            result.status,
            result.exit_code,
            len(body.code),
            duration_ms,
            result.stdout_truncated or result.stderr_truncated,
            extra={"session_id": session_id},
        )
        return result
