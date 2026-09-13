"""Tests for the pure delegation-depth and reverse-dispatch safety checks.

Covers both safety properties this port establishes:

- depth: a delegation chain longer than :data:`MAX_DELEGATION_DEPTH` is
  rejected (a recursive return path that keeps re-extending the chain
  eventually trips this).
- reverse dispatch: dispatching to an agent that is already one of the
  current task's own ancestors is rejected, while a fresh forward dispatch
  to that same agent name (not yet in the chain) is allowed — this is the
  property that lets Polly keep dispatching to ``hermes`` as an ordinary
  coding worker (see ``examples/polly/config.yaml``) while still refusing a
  dispatch that would hand the task back to an upstream orchestrator.
"""

from __future__ import annotations

import pytest

from omnigent.delegation_safety import (
    MAX_DELEGATION_DEPTH,
    detects_reverse_dispatch,
    format_depth_error,
    format_reverse_dispatch_error,
    is_depth_safe,
)


@pytest.mark.parametrize("depth", [0, 1, MAX_DELEGATION_DEPTH])
def test_depth_within_bound_is_safe(depth: int) -> None:
    assert is_depth_safe(depth) is True


@pytest.mark.parametrize("depth", [MAX_DELEGATION_DEPTH + 1, MAX_DELEGATION_DEPTH + 5, 999])
def test_depth_beyond_bound_is_unsafe(depth: int) -> None:
    assert is_depth_safe(depth) is False


def test_custom_max_depth_is_honored() -> None:
    assert is_depth_safe(2, max_depth=1) is False
    assert is_depth_safe(1, max_depth=1) is True


def test_recursive_return_path_beyond_allowed_depth_is_rejected() -> None:
    """
    Simulates a dispatched agent's output looping back to re-trigger
    dispatch: each "return" extends the chain by one hop. Once the chain
    passes the allowed depth, the guard must reject it.
    """
    chain_depth = 0
    for _ in range(MAX_DELEGATION_DEPTH + 1):
        assert is_depth_safe(chain_depth) is True
        chain_depth += 1  # a sub-agent's result triggers another dispatch
    # One more recursive hop pushes the chain past the allowed depth.
    assert is_depth_safe(chain_depth) is False
    assert str(MAX_DELEGATION_DEPTH) in format_depth_error(chain_depth)
    assert str(chain_depth) in format_depth_error(chain_depth)


def test_forward_dispatch_to_a_fresh_agent_is_not_a_reverse_dispatch() -> None:
    """Polly dispatching to hermes as an ordinary worker stays allowed."""
    ancestor_agents = ("omnigent", "polly")
    assert detects_reverse_dispatch(ancestor_agents, "hermes") is False


def test_dispatch_back_to_an_ancestor_is_a_reverse_dispatch() -> None:
    """Hermes handed Polly the task; Polly dispatching back to hermes loops."""
    ancestor_agents = ("hermes", "omnigent", "polly")
    assert detects_reverse_dispatch(ancestor_agents, "hermes") is True


def test_dispatch_back_to_the_immediate_caller_is_a_reverse_dispatch() -> None:
    ancestor_agents = ("polly",)
    assert detects_reverse_dispatch(ancestor_agents, "polly") is True


def test_reverse_dispatch_error_names_target_and_chain() -> None:
    ancestor_agents = ("hermes", "omnigent", "polly")
    message = format_reverse_dispatch_error("hermes", ancestor_agents)
    assert "hermes" in message
    assert "hermes -> omnigent -> polly -> hermes" in message
