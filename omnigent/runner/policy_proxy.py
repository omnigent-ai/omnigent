"""Runner proxy for harness policy evaluations against the Omnigent server.

The harness emits ``policy_evaluation.requested``; the runner forwards it to
``POST /v1/sessions/{id}/policies/evaluate`` and posts the verdict back to the
harness as a ``policy_verdict`` event.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpcore
import httpx

from omnigent.policies.types import FAIL_CLOSED_PHASES
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger("omnigent.runner.app")

# Read budget for runner→server POSTs that can PARK behind a human-approval
# ASK gate: policy evaluation (``_evaluate_policy_via_omnigent``) and sub-agent
# wake-notice delivery (``_deliver_subagent_wake_post``). Both are gated at the
# recipient's REQUEST/LLM/TOOL phase, which can hold for the deciding policy's
# ``ask_timeout`` (default one day). Held at one day (86400s) — matching that
# default — so the POST WAITS for the real verdict instead of severing the
# parked gate at a short read timeout. A 30s cut previously fail-closed to DENY
# (and the wake POST retried into duplicate approval cards). Fast connect (30s)
# so an unreachable server still fails out promptly into the caller's
# fail-open/retry path. Guarded by tests/test_ask_timeout.py.
_ASK_GATE_DELIVERY_READ_TIMEOUT_S: float = 86400.0
_ASK_GATE_DELIVERY_TIMEOUT = httpx.Timeout(_ASK_GATE_DELIVERY_READ_TIMEOUT_S, connect=30.0)


# Transport errors that mean the harness channel is dead (subprocess killed,
# connection reset, timeout). A dead channel can never resolve the harness's
# parked policy future, so we signal recovery instead of log-and-swallow.
_DEAD_HARNESS_CHANNEL_ERRORS: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.StreamClosed,
    httpx.ConnectError,
    httpx.TimeoutException,
    httpcore.ReadError,
    httpcore.ConnectError,
    httpcore.TimeoutException,
)


async def _evaluate_policy_via_omnigent(
    *,
    server_client: httpx.AsyncClient,
    harness_client: httpx.AsyncClient,
    conversation_id: str,
    evaluation_id: str,
    phase: str,
    data: dict[str, Any],
    on_delivery_failure: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """
    Proxy a policy evaluation request from the harness to the Omnigent server.

    Called by the runner's ``proxy_stream`` when it intercepts a
    ``policy_evaluation.requested`` SSE event from the harness. Posts
    the evaluation request to the Omnigent server's
    ``POST /sessions/{id}/policies/evaluate`` endpoint, then delivers
    the verdict back to the harness as a ``policy_verdict`` inbound
    event.

    On failure (AP unreachable, non-200, malformed response) the default
    verdict is phase-aware:

    - ``PHASE_LLM_REQUEST`` / ``PHASE_LLM_RESPONSE`` fail OPEN
      (``POLICY_ACTION_ALLOW``) so a transient Omnigent outage does not
      hang the turn — these gates are advisory.
    - ``PHASE_TOOL_CALL`` fails CLOSED (``POLICY_ACTION_DENY``). For
      connector-native MCP tools the harness ``can_use_tool`` callback
      (which consumes this verdict) is the *only* enforcement point — the
      call is never re-checked server-side — so a policy that cannot be
      evaluated must not let the tool through.
    - ``PHASE_TOOL_RESULT`` fails OPEN: by the result phase the tool has
      already executed, so denying would only block an already-incurred
      side effect.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param harness_client: HTTP client pointed at the harness subprocess.
    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param evaluation_id: Unique correlation id from the harness,
        e.g. ``"poleval_abc123"``.
    :param phase: Proto-style phase string, e.g.
        ``"PHASE_LLM_REQUEST"``.
    :param data: Event data dict for the policy engine.
    :param on_delivery_failure: Called with *conversation_id* when the verdict
        cannot be delivered after retry; wired by callers to cancel the wedged turn.
    """
    # Default verdict on error / non-200 / timeout. Phase-aware: TOOL_CALL
    # fails CLOSED (this round-trip is the authoritative gate for
    # connector-native tools), while advisory LLM phases and TOOL_RESULT
    # (the tool already ran) fail OPEN so a transient outage never hangs
    # the turn.
    _fail_closed = phase in FAIL_CLOSED_PHASES
    _default_action = "POLICY_ACTION_DENY" if _fail_closed else "POLICY_ACTION_ALLOW"
    verdict_action = _default_action
    verdict_reason: str | None = (
        f"Omnigent policy evaluation unavailable; failing closed for {phase}."
        if _fail_closed
        else None
    )
    verdict_data: _JsonObject | None = None

    try:
        ap_resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies/evaluate",
            json={
                "event": {
                    "type": phase,
                    "data": data,
                },
            },
            # A TOOL_CALL/LLM_REQUEST/REQUEST ASK parks server-side in
            # ``_hold_native_ask_gate`` until a human resolves it (up to the
            # deciding policy's ``ask_timeout``, default one day). A 30s read
            # budget here severed that long-poll after 30s — the server saw an
            # UPSTREAM DISCONNECT and failed the gate closed (DENY), so the
            # main (claude-sdk) agent's approval card auto-resolved while
            # native sub-agents (whose hooks already wait the full day) parked
            # correctly. Hold the read budget at one day to match the native
            # hooks' ``_EVALUATE_POLICY_TIMEOUT_S``; the server's ``ask_timeout``
            # remains the single real cap. Fast connect so an unreachable
            # server still fails out promptly into the fail-open path below.
            timeout=_ASK_GATE_DELIVERY_TIMEOUT,
        )
        if ap_resp.status_code == 200:
            result = ap_resp.json()
            # A well-formed 200 carries "result"; a malformed body that
            # omits it falls back to _default_action — i.e. DENY on a
            # tool-call phase. That's deliberate: a 200 we can't read is
            # an unevaluable verdict, which fails closed like any other.
            verdict_action = result.get("result", _default_action)
            verdict_reason = result.get("reason")
            verdict_data = result.get("data")
        else:
            _logger.warning(
                "AP policy evaluate returned %d for %s; defaulting to %s",
                ap_resp.status_code,
                evaluation_id,
                _default_action,
                extra={"session_id": conversation_id},
            )
    except Exception:  # noqa: BLE001 — fail-open (LLM phases) / fail-closed (tool phases)
        _logger.warning(
            "AP policy evaluate failed for %s; defaulting to %s",
            evaluation_id,
            _default_action,
            exc_info=True,
            extra={"session_id": conversation_id},
        )

    # Post the verdict back to the harness as a policy_verdict event.
    verdict_body: dict[str, Any] = {
        "type": "policy_verdict",
        "evaluation_id": evaluation_id,
        "action": verdict_action,
    }
    if verdict_reason is not None:
        verdict_body["reason"] = verdict_reason
    if verdict_data is not None:
        verdict_body["data"] = verdict_data

    # Retry once on dead-channel / timeout / non-2xx; any unacknowledged verdict
    # eventually calls on_delivery_failure to cancel the wedged turn. Track the
    # failure mode so the wrap-up log attributes the cause instead of lumping
    # every mode into one unattributed record.
    failure_reason = "unexpected"
    for _attempt in range(2):
        try:
            resp = await harness_client.post(
                f"/v1/sessions/{conversation_id}/events",
                json=verdict_body,
                timeout=30.0,
            )
        except _DEAD_HARNESS_CHANNEL_ERRORS as exc:
            failure_reason = "dead_channel"
            _logger.warning(
                "Policy verdict %s delivery hit a dead harness channel (attempt %d/2): %s",
                evaluation_id,
                _attempt + 1,
                exc,
                extra={"session_id": conversation_id},
            )
            continue
        except Exception:  # noqa: BLE001 — non-transport: no retry, but still signal
            failure_reason = "unexpected"
            _logger.warning(
                "Failed to deliver policy verdict %s to harness (unexpected error)",
                evaluation_id,
                exc_info=True,
                extra={"session_id": conversation_id},
            )
            break
        if 200 <= resp.status_code < 300:
            return
        failure_reason = f"http_{resp.status_code}"
        _logger.warning(
            "Policy verdict %s delivery got HTTP %d — harness did not accept it (attempt %d/2)",
            evaluation_id,
            resp.status_code,
            _attempt + 1,
            extra={"session_id": conversation_id},
        )

    if failure_reason == "dead_channel":
        # The harness channel died before the verdict could land — an upstream
        # disconnect/teardown consequence whose primary failure (the harness
        # death) is surfaced by stream teardown, not an Omnigent defect. Log at
        # WARNING with a structured reason; the desync recovery still runs.
        _logger.warning(
            "Policy verdict %s undeliverable after retry: harness channel is dead "
            "(upstream disconnect/teardown); signaling desync for %s",
            evaluation_id,
            conversation_id,
            extra={
                "session_id": conversation_id,
                "delivery_failure_reason": "verdict_delivery_channel_dead",
            },
        )
    else:
        # A live harness refused the verdict (non-2xx) or delivery failed in an
        # unforeseen way — potentially a real protocol defect, kept at ERROR.
        _logger.error(
            "Policy verdict %s delivery unacknowledged (%s) after retry; signaling desync for %s",
            evaluation_id,
            failure_reason,
            conversation_id,
            extra={
                "session_id": conversation_id,
                "delivery_failure_reason": failure_reason,
            },
        )
    if on_delivery_failure is not None:
        await on_delivery_failure(conversation_id)
