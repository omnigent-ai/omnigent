"""Tests for the built-in sub-agent depth bound
(:mod:`omnigent.policies.builtins.orchestration` — the ``spawn_depth_bounds``
factory).

``spawn_bounds`` caps dispatches per turn, and every dispatched child gets its
own turn and its own fresh counter, so a chain where each level delegates is
unbounded by it (5^n sessions at depth *n*, each level "inside" its own cap).

``spawn_depth_bounds`` is the structural bound instead: its parameter is the
number of levels still allowed *below* the session it is attached to, and the
runner's spawn path hands each child the parent's allowance minus one level
(``_child_spawn_depth_policy_body`` in :mod:`omnigent.runner.tool_dispatch`),
so one declaration on the root bounds the whole tree however self-similar the
delegation is.

A failure here means either the bound stopped refusing at the declared limit —
an unattended tree can then grow without anything noticing — or it started
refusing dispatches that were declared acceptable.
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.orchestration import spawn_depth_bounds


def _dispatch() -> dict[str, object]:
    """Build a ``sys_session_send`` ``tool_call`` event.

    :returns: An event dict shaped like the policy engine delivers, i.e.
        ``{"type": "tool_call", "data": {"name": ..., "arguments": {...}}}``.
    """
    return {
        "type": "tool_call",
        "data": {"name": "sys_session_send", "arguments": {"agent": "impl_claude"}},
    }


def test_spawn_depth_bounds_allows_inside_the_allowance() -> None:
    """A session with levels left below it may dispatch sub-agents.

    The default allowance (used by a bare ``function: <handler>`` declaration)
    and an explicit one both have to allow the dispatch: refusing here would
    break the delegation the bound is supposed to bound rather than forbid.
    """
    assert spawn_depth_bounds()(_dispatch())["result"] == "ALLOW"
    assert spawn_depth_bounds(max_depth=2)(_dispatch())["result"] == "ALLOW"


def test_spawn_depth_bounds_denies_once_the_allowance_is_exhausted() -> None:
    """``max_depth=0`` refuses every dispatch from that session.

    This is the enforcement the issue asks for: once the tree has reached the
    declared depth, the deepest session cannot dispatch, so an unattended chain
    cannot keep delegating. The reason has to point at the bound and at how to
    widen it, because the refusing session did not declare it.
    """
    decision = spawn_depth_bounds(max_depth=0)(_dispatch())
    assert decision["result"] == "DENY"
    reason = str(decision["reason"])
    assert "depth" in reason
    assert "max_depth" in reason


def test_spawn_depth_bounds_only_gates_dispatch_tools() -> None:
    """Non-dispatch tool calls are untouched, and ``dispatch_tools`` is honoured.

    A failure means the bound is keyed on the wrong tool set: either it would
    block ordinary work in the deepest session, or a deployment that dispatches
    through a differently named tool would be unguarded.
    """
    shell = {"type": "tool_call", "data": {"name": "sys_os_shell", "arguments": {}}}
    assert spawn_depth_bounds(max_depth=0)(shell)["result"] == "ALLOW"

    custom = {"type": "tool_call", "data": {"name": "sys_timer", "arguments": {}}}
    gated = spawn_depth_bounds(max_depth=0, dispatch_tools=["sys_timer"])
    assert gated(custom)["result"] == "DENY"
    assert gated(_dispatch())["result"] == "ALLOW"  # no longer a dispatch tool


def test_spawn_depth_bounds_requires_a_non_negative_integer() -> None:
    """A malformed allowance fails loudly instead of silently disabling the bound.

    A negative or non-integer ``max_depth`` would otherwise resolve to a bound
    that never (or always) denies — a configured guardrail silently doing nothing
    is the failure mode this guards against.
    """
    for invalid in (-1, 1.5, "2", None, True):
        with pytest.raises(ValueError):
            spawn_depth_bounds(max_depth=invalid)  # type: ignore[arg-type]
