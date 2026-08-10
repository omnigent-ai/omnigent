"""Durable session-lifecycle-event production (OMN-104).

Writes stable-ID ``session.completed`` / ``session.failed`` /
``session.awaiting_decision`` / ``session.resumed`` events into the
transactional outbox (:mod:`omnigent.stores.session_lifecycle_store`)
*before* delivery to the configured manager webhook, so a dropped
network call never silently drops the event — the dispatcher
(``omnigent/server/manager_webhook_dispatcher.py``) retries until a 2xx.

This is a deliberately separate module from
:mod:`omnigent.server.session_live_state`, not a mode of it: that module's
own docstring states its writes are best-effort ("a failed write logs and
is dropped; ... the next transition rewrites it") — correct for a display
mirror, wrong for a lifecycle event, which has no "next transition" that
repairs a silently dropped one. Every function here writes synchronously
and lets a failure propagate to its caller rather than swallowing it,
matching the producer-transaction shape in
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §4.3.

No-op until :func:`configure` wires a store (the server app does this at
startup); the runner process and unit tests that never configure it are
unaffected — matching :mod:`omnigent.server.session_live_state`'s pattern.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.db.utils import now_epoch
from omnigent.entities import SessionElicitation
from omnigent.runtime.telemetry import _REDACT_KEY_SUBSTRINGS
from omnigent.stores.session_lifecycle_store import SessionLifecycleStore

_logger = logging.getLogger(__name__)

# Fixed, arbitrary namespace for deriving deterministic event ids — stable
# across processes and restarts (unlike uuid4), so a retried producer call
# computes the *same* event_id and resolves to the same outbox row via its
# primary key rather than a separate dedupe query.
_EVENT_ID_NAMESPACE = uuid.UUID("6f1b7c3a-8d2e-4a5f-9c1b-2e3f4a5b6c7d")

# Allowlisted fields for session.awaiting_decision's ``request`` payload.
# Deliberately an ALLOWLIST, not the denylist below: ElicitationRequestParams
# has ``model_config = ConfigDict(extra="allow")`` (MCP passthrough), so an
# unknown field (a raw env dict, process arguments, headers, secrets) must be
# structurally excluded rather than relying on recognizing its key name.
# tool_input / content_preview are deliberately excluded — they can carry
# arbitrary tool-call arguments, not decision-context fields.
_ELICITATION_REQUEST_ALLOWLIST = (
    "mode",
    "message",
    "requestedSchema",
    "url",
    "phase",
    "policy_name",
    "target_session_id",
    "tool_name",
)

# Allowlisted fields for session.resumed's ``decision`` payload (mirrors
# ElicitationResult — action + the submitted form content).
_ELICITATION_DECISION_ALLOWLIST = (
    "action",
    "content",
)

_store: SessionLifecycleStore | None = None


def configure(store: SessionLifecycleStore | None) -> None:
    """
    Wire (or clear) the store lifecycle events are written to.

    :param store: The server's session-lifecycle store, or ``None`` to
        disable production (tests / non-server processes).
    """
    global _store
    _store = store


def _deterministic_event_id(workspace_id: int, session_id: str, transition_key: str) -> str:
    """Derive a stable ``event_id`` from the fact being reported, not randomness."""
    name = f"{workspace_id}:{session_id}:{transition_key}"
    return uuid.uuid5(_EVENT_ID_NAMESPACE, name).hex


def _redact_denylist(value: dict[str, Any]) -> dict[str, Any]:
    """Apply the existing key-substring denylist filter to a flat dict."""
    return {
        k: ("[redacted]" if any(s in k.lower() for s in _REDACT_KEY_SUBSTRINGS) else v)
        for k, v in value.items()
    }


def _truncate(value: str) -> str:
    if len(value) <= LABEL_VALUE_MAX_LEN:
        return value
    return value[: LABEL_VALUE_MAX_LEN - 1] + "…"


def _allowlist(source: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    return {k: source[k] for k in allowed if k in source and source[k] is not None}


def record_session_completed(
    *,
    workspace_id: int,
    session_id: str,
    response_id: str,
    background_task_count: int | None = None,
) -> None:
    """
    Durably record a ``session.completed`` event.

    Bound to the ``_publish_status`` edge into ``idle`` when a turn was
    actually open (``response_id`` read from
    ``_session_active_response_cache`` before its ``.pop()``). No secondary
    gate on ``background_task_count`` — see
    ``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §5.1.

    :param workspace_id: Tenant partition key.
    :param session_id: The session that just completed a turn.
    :param response_id: The turn's response id (also the transition key
        source) — establishes that a turn was genuinely open.
    :param background_task_count: Informational only; never a gating
        condition.
    """
    if _store is None:
        return
    transition_key = f"turn:{response_id}:completed"
    payload: dict[str, Any] = {"response_id": response_id}
    if background_task_count is not None:
        payload["background_task_count"] = background_task_count
    now = now_epoch()
    _store.record_lifecycle_event(
        event_id=_deterministic_event_id(workspace_id, session_id, transition_key),
        session_id=session_id,
        event_type="session.completed",
        transition_key=transition_key,
        payload=json.dumps(payload),
        now=now,
    )


def record_session_failed(
    *,
    workspace_id: int,
    session_id: str,
    response_id: str,
    error_code: str,
    error_message: str,
) -> None:
    """
    Durably record a ``session.failed`` event.

    Bound to any ``_publish_status`` edge into ``failed`` (not swallowed by
    the sticky failed->idle guard). ``error_code``/``error_message`` are the
    same ``ErrorDetail`` fields already passed to ``_publish_status``,
    passed through the denylist redaction filter plus a length clamp — see
    ``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §3.2.

    :param workspace_id: Tenant partition key.
    :param session_id: The session that just failed.
    :param response_id: The turn's response id (transition key source).
    :param error_code: ``ErrorDetail.code`` verbatim.
    :param error_message: ``ErrorDetail.message`` verbatim (redacted here).
    """
    if _store is None:
        return
    transition_key = f"turn:{response_id}:failed"
    reason = _redact_denylist({"code": error_code, "message": error_message})
    reason["message"] = _truncate(str(reason["message"]))
    payload = {"response_id": response_id, "reason": reason}
    now = now_epoch()
    _store.record_lifecycle_event(
        event_id=_deterministic_event_id(workspace_id, session_id, transition_key),
        session_id=session_id,
        event_type="session.failed",
        transition_key=transition_key,
        payload=json.dumps(payload),
        now=now,
    )


def record_elicitation_raised(
    *,
    workspace_id: int,
    session_id: str,
    elicitation_id: str,
    request: dict[str, Any],
) -> None:
    """
    Durably record a raised elicitation: the ``session_elicitations`` ledger
    row and its ``session.awaiting_decision`` outbox row, in one transaction.

    :param workspace_id: Tenant partition key.
    :param session_id: The session raising the elicitation.
    :param elicitation_id: Correlation id for this request.
    :param request: The elicitation request fields (e.g.
        ``ElicitationRequestParams.model_dump()`` plus ``tool_name``); only
        the allowlisted subset is persisted — see
        ``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §3.2.
    """
    if _store is None:
        return
    allowlisted = _allowlist(request, _ELICITATION_REQUEST_ALLOWLIST)
    transition_key = f"elicitation:{elicitation_id}:awaiting_decision"
    now = now_epoch()
    _store.record_elicitation_raised(
        elicitation_id=elicitation_id,
        session_id=session_id,
        request_payload=json.dumps(allowlisted),
        outbox_event_id=_deterministic_event_id(workspace_id, session_id, transition_key),
        transition_key=transition_key,
        outbox_payload=json.dumps({"elicitation_id": elicitation_id, "request": allowlisted}),
        now=now,
    )


def get_elicitation(elicitation_id: str) -> SessionElicitation | None:
    """Return the durable elicitation ledger row, or ``None`` if untracked / unconfigured."""
    if _store is None:
        return None
    return _store.get_elicitation(elicitation_id)


def record_decision(
    *,
    elicitation_id: str,
    decision: dict[str, Any],
    decided_by: str | None,
) -> SessionElicitation | None:
    """
    Durably record a decision verdict on an elicitation.

    Always called first, unconditionally, before any attempt to resolve an
    in-memory ``Future`` — see
    ``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §5.4.
    Idempotent: a duplicate call after the verdict is already recorded is a
    no-op (handled by the store).

    :param elicitation_id: The elicitation being decided.
    :param decision: The decision fields (e.g. ``ElicitationResult.model_dump()``);
        only the allowlisted subset is persisted.
    :param decided_by: Manager identity from the signed callback, or ``None``
        for a web-UI verdict.
    :returns: The elicitation ledger row after the write, or ``None`` when
        this id has no durable ledger entry at all (never registered via
        :func:`record_elicitation_raised` — a legacy/untracked elicitation)
        or the store isn't configured. The caller uses this to distinguish
        "nothing durable to classify" from a genuine tracked decision.
    """
    if _store is None:
        return None
    allowlisted = _allowlist(decision, _ELICITATION_DECISION_ALLOWLIST)
    return _store.record_decision(
        elicitation_id,
        decision_payload=json.dumps(allowlisted),
        decided_by=decided_by,
        now=now_epoch(),
    )


def record_elicitation_resumed(*, workspace_id: int, elicitation_id: str) -> bool:
    """
    Durably record ``session.resumed`` — ONLY on the runner's truthful
    acknowledgement of having consumed the decision, never on the manager
    HTTP call merely being accepted.

    Marks the elicitation ``delivered_to_runner`` and inserts the
    ``session.resumed`` outbox row in one transaction. No-op (returns
    ``False``) if the elicitation isn't in ``decided`` state (already
    resumed, or never decided) — see
    ``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §5.4.

    :param workspace_id: Tenant partition key.
    :param elicitation_id: The elicitation that was just consumed by the
        runner.
    :returns: ``True`` if this call produced the ``session.resumed`` event
        (i.e. it was the first to observe the runner's ack); ``False`` on an
        idempotent no-op.
    """
    if _store is None:
        return False
    existing = _store.get_elicitation(elicitation_id)
    if existing is None:
        return False
    decision_payload = json.loads(existing.decision_payload) if existing.decision_payload else {}
    transition_key = f"elicitation:{elicitation_id}:resumed"
    now = now_epoch()
    _, _, inserted = _store.record_elicitation_resolved(
        elicitation_id,
        outbox_event_id=_deterministic_event_id(workspace_id, existing.session_id, transition_key),
        transition_key=transition_key,
        outbox_payload=json.dumps(
            {"elicitation_id": elicitation_id, "decision": decision_payload}
        ),
        resolved_at=now,
    )
    return inserted
