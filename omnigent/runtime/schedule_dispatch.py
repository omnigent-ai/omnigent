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
) -> Callable[[Schedule], Awaitable[None]]:
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

    async def fire(schedule: Schedule) -> None:
        """Inject ``schedule.prompt`` as a user message into its conversation.

        :param schedule: The loop schedule that just fired. ``monitor`` kinds
            and prompt-less rows are no-ops (monitors live on the host side).
        """
        if schedule.kind != "loop" or not schedule.prompt:
            return
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
                    f"/v1/sessions/{schedule.conversation_id}/events",
                    json=body,
                    headers=headers,
                )
        except Exception:  # a transport error must not kill the loop
            _logger.exception(
                "scheduler: fire POST errored for schedule %s (conversation %s)",
                schedule.id,
                schedule.conversation_id,
            )
            return
        if resp.status_code in _SOFT_SKIP_STATUSES:
            _logger.info(
                "scheduler: schedule %s fired but no live runner for conversation "
                "%s (HTTP %s) — skipping this tick",
                schedule.id,
                schedule.conversation_id,
                resp.status_code,
            )
            return
        if resp.status_code >= 400:
            _logger.warning(
                "scheduler: fire for schedule %s → HTTP %s: %s",
                schedule.id,
                resp.status_code,
                resp.text[:300],
            )
            return
        _logger.info(
            "scheduler: fired schedule %s into conversation %s",
            schedule.id,
            schedule.conversation_id,
        )

    return fire
