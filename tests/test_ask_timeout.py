"""Drift guard for the policy-ASK delivery timeouts.

A policy ASK is a human-in-the-loop gate: the verdict is delivered
synchronously over a runner→server connection while the server parks the
gate for up to the deciding policy's ``ask_timeout`` (default one day,
``DEFAULT_ASK_TIMEOUT``). The cost-policy bug was that several of those
delivery clients used short read timeouts (30s / 120s / 35s) — far below
``ask_timeout`` — so they severed the parked gate before any human answered,
which fail-closed to DENY (and the sub-agent wake POST retried into duplicate
approval cards).

This pins every such delivery budget to the one-day ASK budget so no client
caps the wait before the policy does. ``connect`` stays fast (30s) so an
unreachable server still fails out promptly. If any delivery budget drops
below ``DEFAULT_ASK_TIMEOUT`` again, these fail loudly.
"""

import omnigent.runner.app as runner_app
import omnigent.runner.pending_approvals as pending_approvals
import omnigent.runner.tool_dispatch as tool_dispatch
import omnigent.runtime.harnesses._scaffold as scaffold
from omnigent.spec.types import DEFAULT_ASK_TIMEOUT

# The deciding policy's default ASK budget — one day. Every delivery client
# that can park behind the gate is pinned to this so none caps the wait first.
ONE_DAY = 86400


def test_default_ask_timeout_is_one_day() -> None:
    """Anchor: the policy ASK default is one day."""
    assert DEFAULT_ASK_TIMEOUT == ONE_DAY


def test_ask_gate_delivery_timeouts_hold_the_ask_budget() -> None:
    """Every runner→server client that PARKS behind a human-approval gate holds
    its read budget at the one-day ASK budget (fast connect kept).

    These are the exact paths whose short timeouts produced the auto-resolved
    card + duplicate cards: the relay/MCP approval default, the policy-eval +
    sub-agent wake-notice POSTs, the message-send POSTs, and the SDK round-trip.
    """
    # relay / MCP approval park default (was 120s -> auto-refuse).
    assert pending_approvals._DEFAULT_WAIT_SECONDS == ONE_DAY

    # policy-eval + sub-agent wake-notice delivery POSTs (were 30s; the wake
    # POST retried on each timeout -> duplicate cards).
    assert runner_app._ASK_GATE_DELIVERY_READ_TIMEOUT_S == ONE_DAY
    assert runner_app._ASK_GATE_DELIVERY_TIMEOUT.read == ONE_DAY
    assert runner_app._ASK_GATE_DELIVERY_TIMEOUT.connect == 30.0

    # message-send POSTs to a child/target session (were 30s).
    assert tool_dispatch._ASK_GATE_DELIVERY_READ_TIMEOUT_S == ONE_DAY
    assert tool_dispatch._ASK_GATE_DELIVERY_TIMEOUT.read == ONE_DAY
    assert tool_dispatch._ASK_GATE_DELIVERY_TIMEOUT.connect == 30.0

    # SDK (non-native) policy round-trip gate (was 35s).
    assert scaffold._POLICY_EVAL_TIMEOUT_S == ONE_DAY


def test_no_delivery_budget_undercuts_the_ask_timeout() -> None:
    """The real invariant: no delivery client caps the wait below the policy's
    ASK budget, so the gate is the single thing that decides how long to wait.

    Written relative to ``DEFAULT_ASK_TIMEOUT`` (not a literal) so it keeps
    holding if the default ASK budget is ever retuned.
    """
    for budget in (
        pending_approvals._DEFAULT_WAIT_SECONDS,
        runner_app._ASK_GATE_DELIVERY_TIMEOUT.read,
        tool_dispatch._ASK_GATE_DELIVERY_TIMEOUT.read,
        scaffold._POLICY_EVAL_TIMEOUT_S,
    ):
        assert budget >= DEFAULT_ASK_TIMEOUT
