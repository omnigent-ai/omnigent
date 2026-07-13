"""
Tests for the built-in real-spend-budget policy
(:mod:`omnigent.policies.builtins.cost`) — the ``real_spend_budget``
factory added for AB#2899.

Unlike ``cost_budget`` (in-process LLM token spend), this factory gates on
REAL business spend reported by an external HTTP oracle (the goettl-core
"Harness Status" service's ``/quota`` endpoint). Layers:

- **Layer 1** — direct callable: the fail-open matrix (every oracle
  failure mode must ALLOW, non-negotiable per the story), positive ASK /
  DENY gating on the daily and MTD horizons independently, the ask-approval
  remember/re-ask behavior, non-gated phases abstaining without even
  calling the oracle, and factory-level parameter validation.
- **Layer 2** — registry discovery: the ``POLICY_REGISTRY`` factory entry
  is browsable and its schema validates good / bad params.
- **Layer 3** — the approval-state key is distinct from ``cost_budget``'s
  (FINDING #4): approving one guard's ASK must never suppress the other's.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.policies.builtins.cost import (
    _ASK_APPROVED_KEY,
    _REAL_SPEND_DAILY_ASK_APPROVED_KEY,
    _REAL_SPEND_MTD_ASK_APPROVED_KEY,
    real_spend_budget,
)
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.schema import SESSION_COST_ASK_APPROVED_STATE_KEY, PolicyEvent

_HANDLER = "omnigent.policies.builtins.cost.real_spend_budget"

# A "healthy" quota payload with headroom on both horizons — the ALLOW
# baseline every gating test perturbs.
_QUOTA_OK: dict[str, float] = {
    "spend_today": 1.0,
    "spend_mtd": 10.0,
    "daily_ask": 5.0,
    "daily_limit": 10.0,
    "mtd_ask": 50.0,
    "mtd_limit": 100.0,
}


def _tool(session_state: dict[str, Any] | None = None) -> PolicyEvent:
    """Build a ``tool_call`` :class:`PolicyEvent`.

    :param session_state: Optional persisted state. ``None`` means empty.
    :returns: A ``tool_call`` event dict.
    """
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {"actor": {}},
        "session_state": session_state or {},
    }


def _request(session_state: dict[str, Any] | None = None) -> PolicyEvent:
    """Build a ``request`` :class:`PolicyEvent`.

    :param session_state: Optional persisted state. ``None`` means empty.
    :returns: A ``request`` event dict.
    """
    return {
        "type": "request",
        "target": None,
        "data": "please approve the repair estimate",
        "context": {"actor": {}},
        "session_state": session_state or {},
    }


def _abstain_event(phase: str) -> PolicyEvent:
    """Build a non-gated-phase :class:`PolicyEvent`.

    :param phase: Event type, e.g. ``"response"`` (NOT ``"request"`` or
        ``"tool_call"``, which are gated).
    :returns: An event dict of the given phase.
    """
    return {
        "type": phase,
        "target": None,
        "data": "x",
        "context": {"actor": {}},
        "session_state": {},
    }


def _json_transport(payload: Any, status_code: int = 200) -> httpx.MockTransport:
    """Build a transport that returns a JSON body for any request.

    :param payload: JSON-serializable body.
    :param status_code: HTTP status code to respond with.
    :returns: A configured :class:`httpx.MockTransport`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def _text_transport(body: str, status_code: int = 200) -> httpx.MockTransport:
    """Build a transport that returns a raw (non-JSON) text body.

    :param body: Raw response body text, e.g. ``"not json"``.
    :param status_code: HTTP status code to respond with.
    :returns: A configured :class:`httpx.MockTransport`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handler)


def _raising_transport(exc: Exception) -> httpx.MockTransport:
    """Build a transport whose every request raises *exc*.

    :param exc: The exception instance to raise, e.g.
        ``httpx.ConnectTimeout("timed out")``.
    :returns: A configured :class:`httpx.MockTransport`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(handler)


def _unreachable_transport() -> httpx.MockTransport:
    """Build a transport that fails the test if it is ever called.

    Used to prove a phase is skipped before the oracle is ever fetched.

    :returns: A configured :class:`httpx.MockTransport`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("oracle should not have been fetched for this event")

    return httpx.MockTransport(handler)


# ══════════════════════════════════════════════════════════════════════
# Layer 1 — direct callable: fail-open matrix
# ══════════════════════════════════════════════════════════════════════


def test_allows_on_timeout() -> None:
    """A request timeout must fail OPEN (ALLOW), not block real work."""
    policy = real_spend_budget(transport=_raising_transport(httpx.ConnectTimeout("timed out")))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_on_connection_refused() -> None:
    """A connection error (oracle down) must fail OPEN (ALLOW)."""
    policy = real_spend_budget(
        transport=_raising_transport(httpx.ConnectError("connection refused"))
    )
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_on_non_200_response() -> None:
    """A non-200 response (e.g. 500/404) must fail OPEN (ALLOW)."""
    policy = real_spend_budget(transport=_json_transport(_QUOTA_OK, status_code=500))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_on_malformed_json() -> None:
    """An unparseable body must fail OPEN (ALLOW)."""
    policy = real_spend_budget(transport=_text_transport("not json at all {{{"))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_on_non_object_json() -> None:
    """Valid JSON that isn't an object (e.g. a bare array) fails OPEN."""
    policy = real_spend_budget(transport=_json_transport([1, 2, 3]))
    assert policy(_tool()) == {"result": "ALLOW"}


@pytest.mark.parametrize("missing_key", list(_QUOTA_OK.keys()))
def test_allows_on_missing_key(missing_key: str) -> None:
    """Any missing required key must fail OPEN (ALLOW)."""
    payload = dict(_QUOTA_OK)
    del payload[missing_key]
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}, f"missing {missing_key} should fail open"


@pytest.mark.parametrize("bad_value", [None, "5.0", True, False, [], {}])
def test_allows_on_non_numeric_value(bad_value: Any) -> None:
    """A non-numeric / null / bool value for any field must fail OPEN."""
    payload = dict(_QUOTA_OK)
    payload["daily_ask"] = bad_value
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_on_oracle_error_stub() -> None:
    """The oracle's own ``{"error": ...}`` stub response fails OPEN."""
    policy = real_spend_budget(transport=_json_transport({"error": "not configured yet"}))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_on_error_stub_even_with_other_keys_present() -> None:
    """An ``error`` key takes precedence over otherwise-valid-looking data.

    A stub response might carry partial/placeholder numeric fields
    alongside ``error`` — the gate must still fail open rather than trust
    them.
    """
    payload = dict(_QUOTA_OK)
    payload["error"] = "quota service not configured"
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}


# ══════════════════════════════════════════════════════════════════════
# Layer 1 — direct callable: threshold validation edge cases (FINDING #1)
# ══════════════════════════════════════════════════════════════════════


def test_allows_when_daily_ask_equals_daily_limit() -> None:
    """``daily_ask == daily_limit`` is a degenerate config — fail OPEN."""
    payload = dict(_QUOTA_OK)
    payload["daily_ask"] = payload["daily_limit"] = 10.0
    payload["spend_today"] = 10.0  # would otherwise DENY
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_when_daily_ask_is_zero() -> None:
    """``daily_ask == 0`` is a degenerate config — fail OPEN."""
    payload = dict(_QUOTA_OK)
    payload["daily_ask"] = 0.0
    payload["spend_today"] = 0.0  # would otherwise ASK at ask=0
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_when_mtd_ask_equals_mtd_limit() -> None:
    """``mtd_ask == mtd_limit`` is a degenerate config — fail OPEN."""
    payload = dict(_QUOTA_OK)
    payload["mtd_ask"] = payload["mtd_limit"] = 100.0
    payload["spend_mtd"] = 100.0  # would otherwise DENY
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_when_mtd_ask_is_zero() -> None:
    """``mtd_ask == 0`` is a degenerate config — fail OPEN."""
    payload = dict(_QUOTA_OK)
    payload["mtd_ask"] = 0.0
    payload["spend_mtd"] = 0.0  # would otherwise ASK at ask=0
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_allows_when_daily_limit_is_negative() -> None:
    """A negative daily_limit fails the ``0 < ask < limit`` invariant."""
    payload = dict(_QUOTA_OK)
    payload["daily_ask"] = -5.0
    payload["daily_limit"] = -1.0
    policy = real_spend_budget(transport=_json_transport(payload))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_factory_rejects_non_positive_timeout() -> None:
    """``timeout_s <= 0`` is rejected at factory-build time."""
    with pytest.raises(ValueError, match="timeout_s"):
        real_spend_budget(timeout_s=0)
    with pytest.raises(ValueError, match="timeout_s"):
        real_spend_budget(timeout_s=-1.0)


def test_factory_rejects_empty_oracle_url() -> None:
    """An empty ``oracle_url`` is rejected at factory-build time."""
    with pytest.raises(ValueError, match="oracle_url"):
        real_spend_budget(oracle_url="")


# ══════════════════════════════════════════════════════════════════════
# Layer 1 — direct callable: positive ASK / DENY gating, both horizons
# ══════════════════════════════════════════════════════════════════════


def test_allows_when_both_horizons_have_headroom() -> None:
    """Healthy quota (well under both ask thresholds) → ALLOW."""
    policy = real_spend_budget(transport=_json_transport(_QUOTA_OK))
    assert policy(_tool()) == {"result": "ALLOW"}


def test_daily_ask_threshold_asks() -> None:
    """Daily spend at/over ``daily_ask`` (MTD healthy) → ASK."""
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 5.0  # == daily_ask
    policy = real_spend_budget(transport=_json_transport(payload))
    result = policy(_tool())
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": _REAL_SPEND_DAILY_ASK_APPROVED_KEY, "action": "set", "value": 5.0},
    ]


def test_mtd_ask_threshold_asks_independently() -> None:
    """MTD spend at/over ``mtd_ask`` (daily healthy) → ASK.

    Proves the MTD horizon gates independently of the daily horizon.
    """
    payload = dict(_QUOTA_OK)
    payload["spend_mtd"] = 50.0  # == mtd_ask
    policy = real_spend_budget(transport=_json_transport(payload))
    result = policy(_tool())
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": _REAL_SPEND_MTD_ASK_APPROVED_KEY, "action": "set", "value": 50.0},
    ]


def test_daily_limit_denies() -> None:
    """Daily spend at/over ``daily_limit`` (MTD healthy) → DENY."""
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 10.0  # == daily_limit
    policy = real_spend_budget(transport=_json_transport(payload))
    result = policy(_tool())
    assert result["result"] == "DENY"
    assert "10.00" in result["reason"]


def test_mtd_limit_denies_independently() -> None:
    """MTD spend at/over ``mtd_limit`` (daily healthy) → DENY.

    Proves the MTD horizon denies independently of the daily horizon.
    """
    payload = dict(_QUOTA_OK)
    payload["spend_mtd"] = 100.0  # == mtd_limit
    policy = real_spend_budget(transport=_json_transport(payload))
    result = policy(_tool())
    assert result["result"] == "DENY"
    assert "100.00" in result["reason"]


def test_either_horizon_tripping_deny_wins_over_ask() -> None:
    """DENY on one horizon wins even if the other only trips ASK.

    Daily is over its ask threshold (would ASK alone); MTD is over its
    limit (DENY). The combined result must be DENY, not ASK.
    """
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 6.0  # over daily_ask (5.0), under daily_limit (10.0)
    payload["spend_mtd"] = 100.0  # == mtd_limit → DENY
    policy = real_spend_budget(transport=_json_transport(payload))
    result = policy(_tool())
    assert result["result"] == "DENY"


def test_both_horizons_over_limit_denies_with_both_named() -> None:
    """Both horizons over limit → DENY reason names both."""
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 20.0
    payload["spend_mtd"] = 200.0
    policy = real_spend_budget(transport=_json_transport(payload))
    result = policy(_tool())
    assert result["result"] == "DENY"
    assert "daily" in result["reason"]
    assert "MTD" in result["reason"]


def test_request_phase_is_gated_same_as_tool_call() -> None:
    """The ``request`` phase (text-only turns) is gated identically."""
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 10.0
    policy = real_spend_budget(transport=_json_transport(payload))
    result = policy(_request())
    assert result["result"] == "DENY"


@pytest.mark.parametrize("phase", ["tool_result", "response", "llm_request", "llm_response"])
def test_non_gated_phases_abstain_without_fetching_oracle(phase: str) -> None:
    """Non-gated phases ALLOW without even calling the oracle.

    Uses a transport that raises if invoked, proving the short-circuit
    happens before any HTTP call — an over-limit quota would otherwise
    DENY.
    """
    policy = real_spend_budget(transport=_unreachable_transport())
    assert policy(_abstain_event(phase)) == {"result": "ALLOW"}


def test_approved_ask_does_not_reprompt_same_spend_level() -> None:
    """An approved ASK at the current spend level does not re-prompt."""
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 5.0  # == daily_ask
    policy = real_spend_budget(transport=_json_transport(payload))
    state = {_REAL_SPEND_DAILY_ASK_APPROVED_KEY: 5.0}
    assert policy(_tool(session_state=state)) == {"result": "ALLOW"}


def test_approved_ask_reprompts_once_spend_rises_further() -> None:
    """Spend rising past the previously-approved level re-asks."""
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 7.0  # over daily_ask, higher than the approved 5.0
    policy = real_spend_budget(transport=_json_transport(payload))
    state = {_REAL_SPEND_DAILY_ASK_APPROVED_KEY: 5.0}
    result = policy(_tool(session_state=state))
    assert result["result"] == "ASK"
    assert result["state_updates"] == [
        {"key": _REAL_SPEND_DAILY_ASK_APPROVED_KEY, "action": "set", "value": 7.0},
    ]


def test_declined_ask_reasks_until_approved() -> None:
    """An un-recorded ASK (declined) re-asks on every subsequent call."""
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 5.0
    policy = real_spend_budget(transport=_json_transport(payload))
    first = policy(_tool(session_state={}))
    second = policy(_tool(session_state={}))
    assert first["result"] == "ASK"
    assert second["result"] == "ASK"  # not recorded → re-asks


# ══════════════════════════════════════════════════════════════════════
# Layer 2 — registry discovery
# ══════════════════════════════════════════════════════════════════════


def test_registry_discovers_real_spend_budget() -> None:
    """The real_spend_budget factory is browsable in the policy registry."""
    load_registry()
    by_handler = {e.handler: e for e in get_registry()}
    assert _HANDLER in by_handler
    assert by_handler[_HANDLER].kind == "factory"
    assert by_handler[_HANDLER].params_schema is not None


def test_registry_validates_factory_params() -> None:
    """The registry schema accepts good params and rejects bad ones."""
    load_registry()
    assert validate_factory_params(_HANDLER, None) is None
    assert validate_factory_params(_HANDLER, {}) is None
    assert (
        validate_factory_params(
            _HANDLER, {"oracle_url": "http://localhost:5151/quota", "timeout_s": 1.5}
        )
        is None
    )
    # Wrong type for timeout_s (must be a number, not a string).
    assert validate_factory_params(_HANDLER, {"timeout_s": "fast"}) is not None
    # Unknown param.
    assert validate_factory_params(_HANDLER, {"bogus": 1}) is not None


# ══════════════════════════════════════════════════════════════════════
# Layer 3 — FINDING #4: approval-state key is distinct from cost_budget's
# ══════════════════════════════════════════════════════════════════════


def test_ask_approved_keys_are_distinct_from_cost_budget() -> None:
    """The real-spend approval keys never collide with cost_budget's.

    FINDING #4: an ASK approval on one guard must never silently suppress
    the other guard's prompts. Distinct key names are the mechanism.
    """
    assert _REAL_SPEND_DAILY_ASK_APPROVED_KEY != _ASK_APPROVED_KEY
    assert _REAL_SPEND_MTD_ASK_APPROVED_KEY != _ASK_APPROVED_KEY
    assert _REAL_SPEND_DAILY_ASK_APPROVED_KEY != SESSION_COST_ASK_APPROVED_STATE_KEY
    assert _REAL_SPEND_MTD_ASK_APPROVED_KEY != SESSION_COST_ASK_APPROVED_STATE_KEY
    assert _ASK_APPROVED_KEY == SESSION_COST_ASK_APPROVED_STATE_KEY  # sanity on the import


def test_cost_budget_approval_does_not_suppress_real_spend_ask() -> None:
    """A cost_budget ASK approval in session_state must not suppress this gate.

    Simulates a session_state that has cost_budget's key set (approved past
    some LLM-spend checkpoint) but nothing under this policy's own keys —
    the real-spend ASK must still fire.
    """
    payload = dict(_QUOTA_OK)
    payload["spend_today"] = 5.0  # == daily_ask
    policy = real_spend_budget(transport=_json_transport(payload))
    # cost_budget's key present (as if that guard's ASK was approved), but
    # under a totally different name — must have zero effect here.
    state = {SESSION_COST_ASK_APPROVED_STATE_KEY: 999.0}
    result = policy(_tool(session_state=state))
    assert result["result"] == "ASK"


def test_real_spend_approval_does_not_suppress_cost_budget_ask() -> None:
    """Conversely, approving this guard's ASK must not affect cost_budget.

    Exercises ``cost_budget`` directly with this policy's approval key
    present in session_state (as if only the real-spend guard had been
    approved) — cost_budget must still ASK on its own unapproved checkpoint.
    """
    from omnigent.policies.builtins.cost import cost_budget

    policy = cost_budget(max_cost_usd=5.0, ask_thresholds_usd=[2.0])
    state = {
        _REAL_SPEND_DAILY_ASK_APPROVED_KEY: 999.0,
        _REAL_SPEND_MTD_ASK_APPROVED_KEY: 999.0,
    }
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {"actor": {}, "usage": {"total_cost_usd": 3.0}, "model": "opus"},
        "session_state": state,
    }
    result = policy(event)
    assert result["result"] == "ASK"  # not suppressed by the other guard's key
