"""Tests for the spawn-path inheritance of the sub-agent depth bound (#5169).

``spawn_depth_bounds`` (see :mod:`omnigent.policies.builtins.orchestration`)
bounds how many levels of sub-agents may hang below the session it is attached
to. Enforcement is per session, and every dispatched child is a *new* session
with its own runner gate, so the allowance has to travel with the session: at
spawn, the runner attaches the parent's effective allowance minus one level.

These tests pin the three pieces of that hand-off, which is where a silent
regression would be worst:

- reading the parent's declared allowance out of its agent spec (the seed of the
  chain, and the reason a bare ``function: <handler>`` declaration still means
  the factory default);
- reading an allowance the parent itself inherited (the attachment on the parent
  session is what decrements each generation);
- building the child's attachment from the smaller of the two, clamped at 0.

If any of them regresses, the bound either stops propagating at some depth (the
tree is unbounded again, silently) or refuses delegation at the first level.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from omnigent.policies.builtins.orchestration import (
    SPAWN_DEPTH_DEFAULT_MAX_DEPTH,
    SPAWN_DEPTH_HANDLER,
)
from omnigent.runner.tool_dispatch import (
    _child_spawn_depth_policy_body,
    _spawn_depth_budget_from_attachments,
    _spawn_depth_budget_from_spec,
)
from omnigent.spec.types import AgentSpec, FunctionPolicySpec, FunctionRef, Phase, PhaseSelector


def _spec_with_depth_policy(arguments: dict[str, Any] | None) -> AgentSpec:
    """Build a parent spec whose guardrails declare the depth bound.

    Only ``guardrails.policies`` is read by the accessor under test, so a
    ``SimpleNamespace`` stands in for the full :class:`AgentSpec`.

    :param arguments: The ``function:`` arguments, e.g. ``{"max_depth": 3}``.
        ``None`` models the bare ``function: <handler>`` form.
    :returns: A spec-shaped namespace with one policy.
    """
    policy = FunctionPolicySpec(
        name="depth",
        on=[PhaseSelector(Phase.TOOL_CALL)],
        function=FunctionRef(SPAWN_DEPTH_HANDLER, arguments),
    )
    return cast(AgentSpec, SimpleNamespace(guardrails=SimpleNamespace(policies=[policy])))


def test_spec_budget_reads_declaration_and_factory_default() -> None:
    """The spec declaration seeds the chain; a bare path means the default.

    A failure means either an inherited chain never starts (the root's declared
    bound is invisible to the spawn path) or a bare declaration is read as "no
    bound", which would hand the child an allowance out of nowhere.
    """
    assert _spawn_depth_budget_from_spec(_spec_with_depth_policy({"max_depth": 3})) == 3
    assert (
        _spawn_depth_budget_from_spec(_spec_with_depth_policy(None))
        == SPAWN_DEPTH_DEFAULT_MAX_DEPTH
    )


def test_spec_budget_ignores_specs_without_the_depth_policy() -> None:
    """No depth policy — or no spec at all — means no bound is inherited.

    Returning a value here would attach a depth bound to child sessions of agents
    that never asked for one, i.e. change behaviour for every existing bundle.
    """
    other = cast(
        AgentSpec,
        SimpleNamespace(
            guardrails=SimpleNamespace(
                policies=[
                    FunctionPolicySpec(
                        name="other",
                        on=[PhaseSelector(Phase.TOOL_CALL)],
                        function=FunctionRef(
                            "omnigent.policies.builtins.orchestration.spawn_bounds"
                        ),
                    )
                ]
            ),
        ),
    )
    assert _spawn_depth_budget_from_spec(other) is None
    assert _spawn_depth_budget_from_spec(None) is None
    no_guardrails = cast(AgentSpec, SimpleNamespace(guardrails=None))
    assert _spawn_depth_budget_from_spec(no_guardrails) is None


def test_attachment_budget_parses_both_payload_shapes() -> None:
    """An inherited allowance is read from the policy list, whatever its envelope.

    ``GET /v1/sessions/{id}/policies`` answers with ``{"data": [...]}``; a bare
    list is tolerated. Non-depth handlers and malformed params are ignored rather
    than guessed, because a wrong number here silently moves the bound.
    """
    attached = {
        "name": "__spawn_depth_bounds",
        "handler": SPAWN_DEPTH_HANDLER,
        "factory_params": {"max_depth": 4},
    }
    assert _spawn_depth_budget_from_attachments({"object": "list", "data": [attached]}) == 4
    assert _spawn_depth_budget_from_attachments([attached]) == 4
    assert _spawn_depth_budget_from_attachments({"data": [{"handler": "x.y"}]}) is None
    without_params = {"data": [{"handler": SPAWN_DEPTH_HANDLER}]}
    assert _spawn_depth_budget_from_attachments(without_params) is None
    assert _spawn_depth_budget_from_attachments(None) is None
    assert _spawn_depth_budget_from_attachments({"data": "not-a-list"}) is None


def test_child_body_decrements_the_smallest_declared_allowance() -> None:
    """The child inherits the parent's effective allowance minus one level.

    Taking the minimum is what keeps a self-similar chain bounded: a bundle that
    re-declares the bound on every sub-agent spec cannot raise the allowance an
    inherited attachment already lowered.
    """
    body = _child_spawn_depth_policy_body([3, None])
    assert body is not None
    assert body["handler"] == SPAWN_DEPTH_HANDLER
    assert body["factory_params"] == {"max_depth": 2}
    assert body["enabled"] is True

    deeper = _child_spawn_depth_policy_body([5, 2])
    assert deeper is not None
    assert deeper["factory_params"] == {"max_depth": 1}


def test_child_body_clamps_at_zero_and_skips_unbounded_parents() -> None:
    """An exhausted or absent allowance has defined behaviour.

    ``0`` (the parent is at its last level) must hand the child ``0``, not ``-1``:
    the policy rejects negative allowances, and a parent that may not dispatch
    implies a child that may not either. With no declaration at all nothing is
    attached — the previous unbounded behaviour — so existing bundles are
    unaffected.
    """
    at_limit = _child_spawn_depth_policy_body([1])
    assert at_limit is not None
    assert at_limit["factory_params"] == {"max_depth": 0}
    clamped = _child_spawn_depth_policy_body([0])
    assert clamped is not None
    assert clamped["factory_params"] == {"max_depth": 0}

    assert _child_spawn_depth_policy_body([None, None]) is None
    assert _child_spawn_depth_policy_body([]) is None
