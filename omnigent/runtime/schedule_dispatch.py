"""Fire callback for the scheduler (B2).

The scheduler engine (:mod:`omnigent.runtime.scheduler`) is deliberately
host-agnostic: it knows *when* a cron ``loop`` is due, but the act of turning a
fired schedule into a real agent turn is injected as a ``fire`` callback. This
module builds that callback.

The chosen mechanism is an **in-process ASGI POST** to the session's own
``POST /v1/sessions/{id}/events`` endpoint. Going through the real endpoint —
rather than re-implementing the dispatch glue — means a fired loop reuses the
entire message path that a human message takes: host relaunch, input-policy
evaluation, runner-relay readiness, file resolution, and the runner forward.
Zero duplication of that fragile preamble (``post_event`` is ~1k lines), and
the scheduler stays correct as that path evolves.

A fired turn is attributed to the schedule's creator by setting the trusted
identity header (header-auth deployments; the default for a loopback server).
On cookie/OIDC deployments the header is ignored and an authenticated service
identity would be needed — see :func:`build_inprocess_fire` for the documented
limitation. When no runner is bound to the session the endpoint returns
``RUNNER_UNAVAILABLE``; that is expected (the user closed the session / the host
went offline) and is logged, not raised, so the cron loop endures.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from omnigent.entities.schedule import Schedule

_logger = logging.getLogger(__name__)

# Soft-skip statuses: a fire that lands here means "no live runner to receive
# the turn right now" (offline host, closed/not-found session). Expected for an
# idle session; logged at INFO and swallowed so the loop keeps its cadence.
_SOFT_SKIP_STATUSES = frozenset({404, 409, 410, 503})


def build_inprocess_fire(
    app: Any,
    *,
    identity_header: str,
    reserved_identities: frozenset[str] = frozenset(),
    timeout_s: float = 60.0,
    conversation_store: Any = None,
    agent_store: Any = None,
    tunnel_registry: Any = None,
    permission_store: Any = None,
) -> Callable[[Schedule], Awaitable[str | None]]:
    """Build a scheduler ``fire`` callback that injects the loop's prompt.

    The returned coroutine POSTs the schedule's ``prompt`` as a ``user``
    message to ``/v1/sessions/{conversation_id}/events`` **in-process** (via
    :class:`httpx.ASGITransport`), so the turn runs through the same dispatch
    path as a human message.

    Attribution: when ``schedule.created_by_user_id`` is set, the request
    carries it in ``identity_header`` so header-auth deployments run the turn
    as that user. Cookie/OIDC deployments ignore the header — a scheduled turn
    there would need a minted service token (a documented follow-up); until
    then such a fire surfaces as an auth failure and is logged, not raised.

    :param app: The ASGI application to dispatch into (the FastAPI server
        instance). Captured once; the transport is reused across fires.
    :param identity_header: The trusted identity header name to attribute the
        turn with, e.g. ``"X-Forwarded-Email"`` (see
        :func:`omnigent.server.auth.resolve_auth_header`).
    :param reserved_identities: Creator ids that must NOT be sent as an
        explicit identity header — chiefly the reserved single-user ``"local"``
        sentinel, which header-auth accepts only as the *absent-header*
        fallback and 401s when presented explicitly. For such a creator the
        header is omitted, so identity resolves via that same local fallback.
    :param timeout_s: Per-fire request timeout. A turn may take a while to
        accept (host relaunch); kept generous but bounded so a wedged dispatch
        can't pin the loop forever.
    :returns: An awaitable ``fire(schedule)`` suitable for
        :class:`~omnigent.runtime.scheduler.SchedulerService`.
    """

    async def _spawn_conversation(schedule: Schedule) -> str | None:
        """Create a fresh conversation for a GLOBAL loop's agent + bind a runner.

        Returns the new conversation id, or ``None`` (logged, loop endures)
        when the spawn deps aren't wired, the agent isn't found, or no runner is
        online (a fresh run needs a live host).

        :param schedule: The global loop (``conversation_id is None``,
            ``agent_name`` set) that just fired.
        """
        if conversation_store is None or agent_store is None or tunnel_registry is None:
            _logger.warning(
                "scheduler: global loop %s can't fire — spawn deps not wired", schedule.id
            )
            return None
        if not schedule.agent_name:
            _logger.warning("scheduler: global loop %s has no agent_name", schedule.id)
            return None
        agent = await asyncio.to_thread(agent_store.get_by_name, schedule.agent_name)
        if agent is None:
            _logger.warning(
                "scheduler: global loop %s → agent %r not found; skipping",
                schedule.id,
                schedule.agent_name,
            )
            return None
        creator = schedule.created_by_user_id
        # Bind a runner OWNED BY the loop's creator — never execute the creator's
        # prompt on another user's host. Unowned runners (single-user / dev,
        # owner is None) are usable by anyone.
        runner_id: str | None = None
        for rid in tunnel_registry.online_runner_ids():
            session = tunnel_registry.get(rid)
            owner = session.owner if session is not None else None
            if owner is None or owner == creator:
                runner_id = rid
                break
        if runner_id is None:
            _logger.info(
                "scheduler: global loop %s → no usable runner online; skipping this tick",
                schedule.id,
            )
            return None
        conv = await asyncio.to_thread(
            conversation_store.create_conversation,
            agent_id=agent.id,
            runner_id=runner_id,
            title=schedule.name,
        )
        # Own the fresh run so it's visible to its creator: the session list
        # filters by ``accessible_by``, and normal session creation grants the
        # creator LEVEL_OWNER. Mirror that exactly (ensure_user first, like
        # session create) or the spawned run is orphaned + invisible in the
        # sidebar — and grant would FK-fail on a not-yet-persisted user.
        if permission_store is not None and creator:
            from omnigent.server.auth import LEVEL_OWNER

            await asyncio.to_thread(permission_store.ensure_user, creator)
            await asyncio.to_thread(permission_store.grant, creator, conv.id, LEVEL_OWNER)
        _logger.info(
            "scheduler: global loop %s spawned conversation %s for agent %s (runner %s)",
            schedule.id,
            conv.id,
            schedule.agent_name,
            runner_id,
        )
        return conv.id

    async def fire(schedule: Schedule) -> str | None:
        """Fire a loop: dispatch its prompt into a conversation.

        Conversation-scoped loops fire into their ``conversation_id``; **global**
        loops (``conversation_id is None``) first spawn a fresh session for their
        ``agent_name``. The prompt then runs through the same
        ``POST /v1/sessions/{id}/events`` dispatch path a human message takes.
        ``monitor`` kinds and prompt-less rows are no-ops.

        :param schedule: The loop schedule that just fired.
        :returns: The conversation id a run was actually dispatched into, or
            ``None`` when the tick was a no-op, soft-skip (no runner), or failure
            — so the scheduler stamps ``last_fired_at``/``last_run_id`` only when
            a run truly ran, and a broken deployment doesn't look healthy.
        """
        if schedule.kind != "loop" or not schedule.prompt:
            return None
        target_conv = schedule.conversation_id
        if target_conv is None:
            target_conv = await _spawn_conversation(schedule)
            if target_conv is None:
                return None
        headers: dict[str, str] = {}
        creator = schedule.created_by_user_id
        if creator and creator not in reserved_identities:
            headers[identity_header] = creator
        body = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": schedule.prompt}],
            },
        }
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://omnigent.internal",
                timeout=timeout_s,
            ) as client:
                resp = await client.post(
                    f"/v1/sessions/{target_conv}/events",
                    json=body,
                    headers=headers,
                )
        except Exception:  # a transport error must not kill the loop
            _logger.exception(
                "scheduler: fire POST errored for schedule %s (conversation %s)",
                schedule.id,
                target_conv,
            )
            return None
        if resp.status_code in _SOFT_SKIP_STATUSES:
            _logger.info(
                "scheduler: schedule %s fired but no live runner for conversation "
                "%s (HTTP %s) — skipping this tick",
                schedule.id,
                target_conv,
                resp.status_code,
            )
            return None
        if resp.status_code >= 400:
            _logger.warning(
                "scheduler: fire for schedule %s → HTTP %s: %s",
                schedule.id,
                resp.status_code,
                resp.text[:300],
            )
            return None
        _logger.info(
            "scheduler: fired schedule %s into conversation %s",
            schedule.id,
            target_conv,
        )
        return target_conv

    return fire
