"""Elicitation routes: resolve and get elicitations."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import (
    APIRouter,
    Request,
)

from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    pending_elicitations,
)
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    AuthProvider,
)
from omnigent.server.routes._ask_user_question import (
    build_ask_user_question_params,
    reconstruct_ask_user_question_output,
    validate_ask_user_question_questions,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    _logger,
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import (
    _apply_pending_policy_ask_writes,
)
from omnigent.server.routes._sessions.orchestration import (
    _publish_and_wait_for_harness_elicitation,
    _resolve_elicitation,
)
from omnigent.server.schemas import (
    ElicitationResult,
)
from omnigent.spec.types import DEFAULT_ASK_TIMEOUT
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore


def register_elicitations_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the elicitations routes on router."""

    @router.post(
        "/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        # Internal elicitation flow — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        # response_model=None: the body is a small acknowledgement
        # dict, not a domain model.
        response_model=None,
    )
    async def resolve_elicitation(
        request: Request,
        session_id: str,
        elicitation_id: str,
        body: ElicitationResult,
    ) -> dict[str, bool]:
        """
        Resolve an outstanding elicitation by its URL (URL-based
        elicitation).

        The dedicated, RESTful counterpart to delivering a verdict
        via the ``type == "approval"`` event on
        ``POST /v1/sessions/{id}/events``. An elicitation request
        published in ``mode == "url"`` carries this endpoint's path
        as its ``params.url``; the client hits it directly with the
        MCP :class:`ElicitationResult` body instead of POSTing a
        generic approval event. The verdict routes through the
        shared :func:`_resolve_elicitation`, so resolution semantics
        are identical to the event path.

        The ``elicitation_id`` is taken from the URL rather than the
        body, so the unguessable id (``secrets.token_hex(16)``) is
        the capability scoping the resolution — combined with the
        session-owner ``LEVEL_EDIT`` gate below and the server-side
        ownership check inside :func:`_resolve_elicitation`.

        :param request: The inbound request, used for identity
            extraction.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param elicitation_id: Correlation id of the elicitation to
            resolve, e.g. ``"elicit_abc123"``. Taken from the URL
            path, not the body.
        :param body: The MCP-shaped verdict — ``action``
            (``"accept"`` / ``"decline"`` / ``"cancel"``) plus
            optional form ``content``.
        :returns: ``{"queued": False}`` — resolution is synchronous
            and persists no conversation item.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        _resolve_data = {"elicitation_id": elicitation_id, **body.model_dump(exclude_none=True)}
        await _resolve_elicitation(session_id, _resolve_data, runner_router, conversation_store)
        # Apply any policy writes deferred by the relay tool-call ASK gate
        # (e.g. a cost-budget checkpoint) now that the verdict is in.
        await _apply_pending_policy_ask_writes(
            session_id, conv, conversation_store, agent_store, _resolve_data
        )
        return {"queued": False}

    @router.post(
        "/sessions/{session_id}/ask_user_question",
        # Internal builtin-tool dispatch flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def ask_user_question(
        request: Request,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Run one ``sys_ask_user_question`` builtin-tool call end to end.

        Runner-invoked (see the ``_ASK_USER_QUESTION_TOOLS`` branch of
        ``execute_tool()`` in ``omnigent/runner/tool_dispatch.py``), not
        reachable by the LLM directly. Validates the tool call's
        ``questions`` argument, publishes a
        ``response.elicitation_request`` through the SAME elicitation
        engine every other harness ASK gate uses (
        :func:`_publish_and_wait_for_harness_elicitation` — no bespoke
        plumbing), and blocks until a human answers, declines, or the
        request times out (one day, matching the platform's
        :data:`~omnigent.spec.types.DEFAULT_ASK_TIMEOUT`). The verdict is
        reconstructed into the tool's rich, Claude-Code-shaped return
        value by :func:`~omnigent.server.routes._ask_user_question.reconstruct_ask_user_question_output`.

        :param request: The inbound request, used for identity extraction
            and upstream-disconnect detection inside the parking helper.
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param body: ``{"questions": [...]}`` — the tool call's raw
            arguments, per Claude Code's ``AskUserQuestionInput`` contract
            plus the platform's ``recommended`` option extension.
        :returns: ``{"questions": [...], "answers": {...}}`` (optionally
            with ``response``) on a normal accept, or ``{"questions": [...],
            "answers": {}, "error": "..."}`` on decline / cancel / timeout.
        :raises OmnigentError: 400 if ``questions`` doesn't satisfy the
            1-4 questions / 2-4 options contract; 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        questions = validate_ask_user_question_questions(body.get("questions"))
        params = build_ask_user_question_params(questions)
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=float(DEFAULT_ASK_TIMEOUT),
            conversation_store=conversation_store,
        )
        return reconstruct_ask_user_question_output(questions, result)

    @router.get(
        "/sessions/{session_id}/elicitations/{elicitation_id}",
        # Internal elicitation flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def get_elicitation(
        request: Request,
        session_id: str,
        elicitation_id: str,
    ) -> dict[str, Any]:
        """
        Return the state of a pending elicitation as JSON.

        Used by the frontend's standalone approval page
        (``/approve/:sessionId/:elicitationId``) to fetch the
        elicitation prompt and render approve/reject controls.
        The payload is read from the in-memory
        :mod:`omnigent.runtime.pending_elicitations` index — no
        database persistence required.

        :param request: The inbound request, used for identity
            extraction.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param elicitation_id: Correlation id of the elicitation,
            e.g. ``"elicit_abc123"``.
        :returns: JSON with ``status`` (``"pending"`` or
            ``"resolved"``), and when pending: ``message``,
            ``phase``, ``policy_name``, ``content_preview``.
        :raises OmnigentError: 404 if the session does not exist.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()

        found = pending_elicitations.lookup(elicitation_id)
        if found is None or found[0] != session_id:
            return {"status": "resolved"}

        _conv_id, event = found
        params_value = event.get("params")
        params = params_value if isinstance(params_value, dict) else {}
        return {
            "status": "pending",
            "message": params.get("message", "Approval required"),
            "phase": params.get("phase", ""),
            "policy_name": params.get("policy_name", ""),
            "content_preview": params.get("content_preview", ""),
        }
