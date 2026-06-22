"""
ASK flow — ``_await_elicitation`` helper.

When the engine composes an ASK result, the caller (the workflow
enforcement site) hands the result + identity to this helper. The
helper:

1. Registers an elicitation row in ``pending_tool_calls`` (same
   table client-side tool calls use; ``tool_name`` field carries
   the :data:`ELICITATION_PENDING_TOOL_NAME` sentinel so the
   PATCH route knows not to hand the row to a tool dispatcher).
2. Emits a ``response.elicitation_request`` SSE event on the root
   task's stream — params shape mirrors MCP's
   ``ElicitRequestFormParams`` field-for-field so an MCP client
   can parse it without translation.
3. Parks on the existing ``tool_result`` topic, respecting the
   per-policy / spec-level ASK timeout.
4. On wake, parses the verdict strictly per MCP semantics: only
   ``action == "accept"`` returns True. Anything else
   (``decline`` / ``cancel`` / malformed JSON / missing field /
   timeout) returns False (caller maps to DENY).
5. Applies the ASK-accumulated ``set_labels`` and
   ``state_updates`` **only on approve** (POLICIES.md §7.2
   invariant: a denied / cancelled ASK leaves no side effects).

The wire shape — both the SSE event params block and the
``ElicitationResult`` POST body the consumer replies with — match
MCP's elicitation primitive verbatim. See
:class:`omnigent.server.schemas.ElicitationResult` and
``designs/SERVER_HARNESS_CONTRACT.md`` §"Universal API additions".

The three callback parameters (``register``, ``emit``, ``park``)
are seams that let the helper work in tests without requiring a
full task_store + SSE stack. See POLICIES.md §7, §13.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from omnigent.policies.types import ElicitationRequest, PolicyResult
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import Phase

# Sentinel value for the ``tool_name`` column on a pending row that
# represents an elicitation rather than a real client-side tool call.
# Double-underscore prefix marks it as an internal (non-LLM-facing)
# marker so consumers can distinguish elicitation rows from the
# client-tunneled tool calls the same table also stores.
ELICITATION_PENDING_TOOL_NAME = "__elicitation__"

# JSON-RPC method name MCP uses for elicitation requests. Surfaced
# verbatim on the SSE event envelope so MCP clients can route on the
# method name they already recognize.
_MCP_ELICITATION_METHOD = "elicitation/create"

# Parking callback contract — the workflow binds a real parking
# implementation (recv on the ``tool_result`` topic with the
# per-policy timeout) at wiring time. Tests inject canned awaitables
# that return a verdict string or raise TimeoutError.
_ParkCallback = Callable[[str, int], Awaitable[str | None]]

# SSE publisher contract — the workflow binds a real ``_write_output``
# variant that emits the elicitation_request event onto the root
# task's stream. Tests can pass a no-op or a recorder.
_EmitCallback = Callable[[dict[str, Any]], None]

# Pending-call persister contract — registers the elicitation_id ↔
# task_id mapping so the session approval dispatcher can wake this
# workflow when the verdict arrives. The third argument is the JSON-encoded
# elicitation params (stored in the row's ``arguments`` column for
# inspection / replay).
_RegisterCallback = Callable[[str, str, str], None]


async def _await_elicitation(
    *,
    task_id: str,
    root_task_id: str,
    result: PolicyResult,
    phase: Phase,
    content_preview: str,
    policy_engine: PolicyEngine,
    register: _RegisterCallback,
    emit: _EmitCallback,
    park: _ParkCallback,
) -> bool:
    """
    Drive one elicitation round-trip; return True iff approved.

    Wires the three seams (``register``, ``emit``, ``park``) to the
    production task_store + SSE stack via the workflow integration
    in :func:`omnigent.runtime.workflow._drive_policy_approval`.

    On approve: applies the ASK-accumulated ``set_labels`` from the
    engine's composed result. On refuse / cancel / timeout /
    malformed verdict: returns ``False`` and applies nothing,
    preserving POLICIES.md §7.2 ("no side effects on denied ASK").

    :param task_id: The sub-agent's task ID (the parked workflow).
    :param root_task_id: The root task whose SSE stream receives
        the elicitation event.
    :param result: Composed :class:`PolicyResult` — carries the
        combined reason, deciding_policy, and withheld set_labels.
    :param phase: Which enforcement point produced the ASK.
    :param content_preview: Truncated content snapshot for the UI.
    :param policy_engine: Engine — used to resolve the per-policy
        ``ask_timeout`` override off the deciding policy's spec, and
        to apply label writes on approve.
    :param register: Seam: register the pending elicitation row.
        Called with ``(elicitation_id, inner_task_id, params_json)``.
    :param emit: Seam: publish the ``response.elicitation_request``
        event on the root task's stream.
    :param park: Seam: block until the approval dispatcher delivers
        a verdict or the timeout elapses. Returns the verdict JSON
        string (the MCP ``ElicitResult`` body) or ``None`` if the
        row wasn't completed on wake. Raises ``TimeoutError`` on
        deadline expiry.
    :returns: ``True`` only when the verdict's ``action`` is exactly
        ``"accept"``; ``False`` otherwise (decline / cancel /
        timeout / malformed).
    """
    elicitation_id = f"elicit_{secrets.token_hex(16)}"
    elicitation = ElicitationRequest(
        message=result.reason or "",
        phase=phase.value,
        policy_names=result.deciding_policies or [""],
        content_preview=_truncate(content_preview, limit=1024),
    )
    params_json = build_elicitation_params_json(elicitation)
    register(elicitation_id, task_id, params_json)
    emit(build_elicitation_request_event(elicitation_id, elicitation))

    effective_timeout = resolve_ask_timeout(policy_engine, result)
    try:
        raw_verdict = await park(elicitation_id, effective_timeout)
    except TimeoutError:
        return False

    approved = _parse_verdict(raw_verdict)
    if approved:
        # POLICIES.md §7.2: writes accumulated by ASKing policies
        # land only on approve. On refuse / cancel / timeout /
        # malformed verdict we drop them — a denied ASK must leave
        # no trace.
        if result.set_labels:
            policy_engine.apply_label_writes(result.set_labels)
        if result.state_updates:
            policy_engine.apply_state_updates(result.state_updates)
    return approved


def _resolve_elicitation_mode() -> str:
    """
    Resolve the elicitation mode from the environment once at import time.

    Reads ``OMNIGENT_ELICITATION_MODE`` (``"url"`` or ``"form"``).
    Defaults to ``"url"`` when unset — the standalone approval page
    is the default experience.

    :returns: ``"url"`` or ``"form"``.
    """
    raw = os.environ.get("OMNIGENT_ELICITATION_MODE", "url").lower().strip()
    return raw if raw in ("url", "form") else "url"


# Read once at import time — server restart required to change.
_ELICITATION_MODE: str = _resolve_elicitation_mode()


def build_elicitation_request_event(
    elicitation_id: str,
    elicitation: ElicitationRequest,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Build the ``response.elicitation_request`` SSE payload.

    The ``params`` block is byte-for-byte the MCP
    ``ElicitRequestFormParams`` shape (mode/message/requestedSchema)
    so an MCP client can parse it directly. Policy-context fields
    (``phase``, ``policy_name``, ``content_preview``) live alongside
    the standard MCP fields under ``params`` — MCP's
    ``model_config = ConfigDict(extra="allow")`` permits this without
    breaking strict consumers.

    When ``session_id`` is provided and the configured elicitation mode
    is ``"url"`` (the default), the event carries ``mode: "url"`` with
    a ``url`` field pointing to the standalone approval page at
    ``/approve/{session_id}/{elicitation_id}``.
    The client renders a link instead of inline approve/reject buttons.

    :param elicitation_id: Unique id correlating this request to the
        consumer's :class:`omnigent.server.schemas.ElicitationResult`
        reply, e.g. ``"elicit_abc123"``.
    :param elicitation: The internal request payload.
    :param session_id: Session/conversation id needed to construct
        the approval-page URL. ``None`` on the runner side (workflow
        emit callback) where URL mode is not applicable — falls back
        to ``"form"`` mode.
    :returns: Dict the workflow emits onto the root task's SSE
        stream verbatim.
    """
    mode = "form"
    url = None
    if session_id is not None and _ELICITATION_MODE == "url":
        mode = "url"
        url = f"/approve/{session_id}/{elicitation_id}"

    params: dict[str, Any] = {
        "mode": mode,
        "message": elicitation.message,
        "requestedSchema": elicitation.requested_schema,
        # Extras (allowed by MCP's `extra="allow"` config). Policy
        # context for the renderer; ignored by strict MCP clients.
        "phase": elicitation.phase,
        "policy_name": elicitation.policy_name,
        "content_preview": elicitation.content_preview,
    }
    if len(elicitation.policy_names) > 1:
        params["policy_names"] = elicitation.policy_names
    if url is not None:
        params["url"] = url

    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "method": _MCP_ELICITATION_METHOD,
        "params": params,
    }


def build_elicitation_params_json(elicitation: ElicitationRequest) -> str:
    """
    Serialize the elicitation params block for storage.

    Persists the same shape as the SSE event's ``params`` field so
    a debugger inspecting ``pending_tool_calls.arguments`` sees
    exactly what the consumer was shown. JSON encoding (rather than
    a Python dict) so the column matches the existing
    ``arguments`` column convention used by client-side tool calls.

    :param elicitation: The internal elicitation request.
    :returns: JSON string suitable for the
        ``pending_tool_calls.arguments`` column.
    """
    return json.dumps(
        {
            "mode": "form",
            "message": elicitation.message,
            "requestedSchema": elicitation.requested_schema,
            "phase": elicitation.phase,
            "policy_name": elicitation.policy_name,
            "content_preview": elicitation.content_preview,
        }
    )


def resolve_ask_timeout(
    engine: PolicyEngine,
    result: PolicyResult,
) -> int:
    """
    Pick the effective timeout for this elicitation.

    Per-policy override wins over the spec-level default:
    ``result.deciding_policy`` names the first ASKing policy in YAML
    order; we read its spec's ``ask_timeout`` field and fall back to
    the engine's spec-level ``ask_timeout`` if absent. Used by both
    the in-process ASK gate below and the server's relay policy
    evaluation, whose ``pending`` verdict carries the value so the
    runner's park honors the spec instead of a hard-coded default.

    :param engine: The workflow's engine.
    :param result: Composed ASK result — carries deciding_policy.
    :returns: Timeout in seconds. Always > 0 (spec-load rejects
        ``<= 0`` at parse time).
    """
    deciding_spec = engine.spec_for(result.deciding_policy)
    if deciding_spec is not None and deciding_spec.ask_timeout is not None:
        return deciding_spec.ask_timeout
    return engine.ask_timeout


def _parse_verdict(raw: str | None) -> bool:
    """
    Strict verdict parser — returns True ONLY for
    ``action == "accept"``.

    Fail-closed per POLICIES.md §13: anything else (``decline``,
    ``cancel``, missing ``action``, wrong type, unparseable JSON,
    non-dict root, or ``None`` from a no-row wake) returns False.
    The elicitations POST route stays a dumb pipe — all verdict
    semantics live here, which keeps the route generic.

    :param raw: The verdict string delivered via the park callback —
        the JSON-encoded MCP ``ElicitResult`` body. ``None`` when
        no row was present on wake (cancel races, malformed input).
        Also returns False.
    :returns: ``True`` only on exact ``action == "accept"``.
    """
    if raw is None:
        return False
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return parsed.get("action") == "accept"


def _truncate(text: str, *, limit: int) -> str:
    """
    Truncate content for the elicitation UI preview.

    :param text: Raw content string.
    :param limit: Maximum characters. 1024 keeps the UI readable
        without overwhelming a paginated viewer.
    :returns: Truncated string with a ``" [truncated]"`` marker
        appended when clipping occurred.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + " [truncated]"


__all__ = [
    "ELICITATION_PENDING_TOOL_NAME",
    "_await_elicitation",
    "build_elicitation_params_json",
    "build_elicitation_request_event",
    "resolve_ask_timeout",
]
