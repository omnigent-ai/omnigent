"""Elicitation routes: resolve and get elicitations."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
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
    _resolve_elicitation_durably,
)
from omnigent.server.schemas import (
    ElicitationResult,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import runner_seen_is_fresh
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
        :returns: ``{"queued": False, "resolved": bool, "pending_redelivery": bool}``.
            ``resolved=True`` when the verdict was consumed synchronously
            (no restart, or the runner was reachable on this delivery
            attempt); ``pending_redelivery=True`` when the verdict is
            durably recorded but the runner's tunnel isn't currently
            reachable and appears to have recently reconnected elsewhere
            (OMN-104 §5.4 case (b) — redelivered on tunnel reconnect).
        :raises OmnigentError: 404 if no session exists.
        :raises HTTPException: 410 (``error_code=elicitation_not_resolvable``)
            when the runner/host is not believed to be alive at all
            (OMN-104 §5.4 case (c)) — the verdict is still durably
            recorded (see ``session_elicitations``), but the endpoint does
            not claim the session resumed, because it structurally did not.
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
        # The shared OMN-104 durable sequence (ownership check, durable
        # record_decision BEFORE any attempt to resolve an in-memory Future
        # or reach the runner, then classify + record_elicitation_resumed
        # on a truthful ack) — also used by the generic ``approval`` event
        # branch of POST /v1/sessions/{id}/events, so both entry points
        # that can resolve an elicitation go through identical durability.
        durable = await _resolve_elicitation_durably(
            session_id, _resolve_data, runner_router, conversation_store, decided_by=user_id
        )
        decision_record = durable.decision_record
        if decision_record is None:
            # No durable OMN-104 ledger row for this id — either the feature
            # isn't wired, or (the common case for this shared endpoint) the
            # id was never registered via record_elicitation_raised / is
            # already resolved / is unknown. Preserve the pre-OMN-104
            # contract exactly rather than 410ing a request that always used
            # to be a harmless idempotent no-op.
            await _apply_pending_policy_ask_writes(
                session_id, conv, conversation_store, agent_store, _resolve_data
            )
            return {"queued": False}
        if decision_record.status in ("delivered_to_runner", "expired"):
            # Already truthfully resumed (by an earlier call to this
            # endpoint, or by reconnect redelivery) — idempotent no-op, not
            # a fresh classification. A duplicate resolve must not risk a
            # 410 just because the harness Future it already consumed is
            # now done().
            await _apply_pending_policy_ask_writes(
                session_id, conv, conversation_store, agent_store, _resolve_data
            )
            return {"queued": False, "resolved": True, "pending_redelivery": False}
        resolved = durable.resolved
        pending_redelivery = False
        if not resolved:
            # Not resolved synchronously. Classify by whether the runner is
            # believed alive at all (cross-replica, DB-backed freshness —
            # not this replica's local tunnel registry, which a manager's
            # request may not share with the replica holding the tunnel):
            # fresh -> server restart / this replica's tunnel is stale, but
            # the runner is expected to reconnect soon, so the decided-but-
            # undelivered row is redelivered on that reconnect (see
            # _on_runner_connect in app.py). Not fresh (or no runner at
            # all) -> the runner process itself is not believed alive; no
            # durable row can rebind a coroutine that no longer exists.
            connectivity = await asyncio.to_thread(
                conversation_store.get_session_connectivity, [session_id]
            )
            conn = connectivity.get(session_id)
            now = time.time()
            # a632550e's reconnect grace clears runner_last_seen the INSTANT
            # a runner disconnects — well before the grace it also grants
            # that same runner actually expires. Without this check, a
            # decision arriving inside that window would see stale
            # liveness and be misclassified as "runner dead" even though
            # the runner is very likely still alive and about to
            # reconnect. Consult the grace-pending marker FIRST — durable
            # (SessionConnectivity.runner_disconnect_grace_deadline, written
            # by the replica holding the tunnel at the same moment it
            # clears runner_last_seen; see
            # session_live_state.set_runner_disconnect_grace), so this
            # check is correct regardless of which replica this decision
            # request landed on — before falling back to the cross-replica
            # freshness signal, which is only meaningful once the grace has
            # genuinely elapsed with no reconnect.
            runner_in_disconnect_grace = (
                conn is not None
                and conn.runner_id is not None
                and conn.runner_disconnect_grace_deadline is not None
                and conn.runner_disconnect_grace_deadline > now
            )
            runner_alive = runner_in_disconnect_grace or (
                conn is not None
                and conn.runner_id is not None
                and runner_seen_is_fresh(conn.runner_last_seen, now=int(now))
            )
            if runner_alive:
                pending_redelivery = True
            else:
                raise HTTPException(
                    status_code=410,
                    detail={
                        "error_code": "elicitation_not_resolvable",
                        "message": (
                            "The runner for this session is not reachable and is not "
                            "believed to be alive; the decision was recorded but could "
                            "not be delivered."
                        ),
                    },
                )
        # Apply any policy writes deferred by the relay tool-call ASK gate
        # (e.g. a cost-budget checkpoint) now that the verdict is in.
        await _apply_pending_policy_ask_writes(
            session_id, conv, conversation_store, agent_store, _resolve_data
        )
        return {"queued": False, "resolved": resolved, "pending_redelivery": pending_redelivery}

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
