"""``/btw`` side-question route."""

from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import APIRouter, Request

from omnigent.entities import ConversationItem, NewConversationItem, SideQuestionData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.side_questions.service import (
    SIDE_QUESTION_INFERENCE_TIMEOUT_SECONDS,
    SIDE_QUESTION_MAX_EXCERPT_CHARS,
)
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import require_access as _require_access
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    _logger,
    get_server_runner_router,
    session_stream,
)
from omnigent.server.schemas import (
    OutputItemDoneEvent,
    SessionSideQuestionRequest,
    SessionSideQuestionResponse,
)
from omnigent.server.side_questions import (
    SIDE_QUESTION_ITEM_SCAN_LIMIT,
    build_transcript_excerpt,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

# The runner caps its own inference; allow a little more here so a
# runner-side timeout surfaces as its own 504 rather than as a client
# disconnect that says nothing useful.
_RUNNER_HTTP_TIMEOUT_SECONDS = SIDE_QUESTION_INFERENCE_TIMEOUT_SECONDS + 15.0


def register_side_question_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the side-question route on router."""

    @router.post(
        "/sessions/{session_id}/side-question",
        response_model=SessionSideQuestionResponse,
    )
    async def ask_session_side_question(
        request: Request,
        session_id: str,
        body: SessionSideQuestionRequest,
    ) -> SessionSideQuestionResponse:
        """
        Answer a question about this session without joining it.

        The answer is produced by a throwaway, tool-free process on the
        session's runner and persisted as a ``side_question`` item —
        visible in the transcript, filtered out of the model's history
        on every later turn.

        EDIT is required rather than READ: answering spends the
        session owner's runner and tokens, which a read-only viewer of
        a shared session should not be able to do.

        :param session_id: Session identifier, e.g. ``"conv_abc123"``.
        :param body: The question to ask.
        :returns: The answer plus the persisted item's id, or
            ``status="unsupported"`` when the session's harness has no
            side-question generator.
        :raises OmnigentError: 404 if no session exists; 503 if it has
            no bound runner or the runner is unreachable / times out;
            500 when the runner reports a failure.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()

        question = body.question.strip()
        if not question:
            raise OmnigentError(
                "question must not be blank",
                code=ErrorCode.INVALID_INPUT,
            )

        router_ = runner_router or get_server_runner_router()
        routed = (
            router_.client_for_existing_conversation(session_id) if router_ is not None else None
        )
        if routed is None:
            raise OmnigentError(
                "Session has no running runner to answer a side question",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )

        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            SIDE_QUESTION_ITEM_SCAN_LIMIT,
            None,
            None,
            "desc",
            None,
        )
        # list_items came back newest-first; the excerpt reads oldest-first.
        excerpt = build_transcript_excerpt(
            list(reversed(page.data)),
            max_chars=SIDE_QUESTION_MAX_EXCERPT_CHARS,
        )

        try:
            response = await routed.client.post(
                f"/v1/sessions/{session_id}/side-question",
                json={
                    "question": question,
                    "excerpt": excerpt,
                    "agent_id": conv.agent_id,
                },
                timeout=_RUNNER_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise OmnigentError(
                "Side question timed out",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        except httpx.HTTPError as exc:
            _logger.warning("side question runner call failed for %s: %s", session_id, exc)
            raise OmnigentError(
                "Side question could not reach the runner",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc

        if response.status_code == 504:
            raise OmnigentError("Side question timed out", code=ErrorCode.RUNNER_UNAVAILABLE)
        if response.status_code != 200:
            raise OmnigentError(
                _runner_error_detail(response),
                code=ErrorCode.INTERNAL_ERROR,
            )

        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "answered":
            return SessionSideQuestionResponse(status="unsupported")
        answer = payload.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return SessionSideQuestionResponse(status="unsupported")

        item = await _persist_side_question(
            session_id,
            conversation_store,
            agent=conv.agent_id or "agent",
            question=question,
            answer=answer.strip(),
            created_by=user_id,
        )
        return SessionSideQuestionResponse(
            status="answered",
            answer=answer.strip(),
            item_id=item.id,
        )


def _runner_error_detail(response: httpx.Response) -> str:
    """Pull a client-safe message out of a runner error body."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return f"Side question failed (runner returned HTTP {response.status_code})"


async def _persist_side_question(
    session_id: str,
    conversation_store: ConversationStore,
    *,
    agent: str,
    question: str,
    answer: str,
    created_by: str | None,
) -> ConversationItem:
    """Append the exchange and broadcast it to connected clients.

    Persisted so the aside survives reload and reaches the user's other
    devices; broadcast so a session open in two places doesn't need one
    of them to refresh.
    """
    new_item = NewConversationItem(
        type="side_question",
        response_id=f"turn_{uuid.uuid4().hex}",
        data=SideQuestionData(agent=agent, question=question, answer=answer),
        created_by=created_by,
    )
    persisted = await asyncio.to_thread(conversation_store.append, session_id, [new_item])
    item = persisted[0]
    event = OutputItemDoneEvent(type="response.output_item.done", item=item.to_api_dict())
    session_stream.publish(session_id, event.model_dump())
    return item
