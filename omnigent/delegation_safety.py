"""Pure delegation-depth and reverse-dispatch safety checks.

An orchestrator like ``polly`` dispatches sub-agents via
``sys_session_send`` (:mod:`omnigent.tools.builtins.spawn`). Each dispatch
creates (or continues) a child conversation; nothing currently caps how
many hops such a chain may grow (Hermes hands Polly a task -> Polly
dispatches a worker -> that worker's spec could itself declare further
sub-agents -> ...), and nothing currently rejects a dispatch that would
route back to an agent already present earlier in that same chain, e.g.
Hermes -> Omnigent -> Polly -> Hermes again.

This module intentionally has no I/O and does not touch the
conversation/session tree itself
(``Conversation.parent_conversation_id`` / ``root_conversation_id``, see
:mod:`omnigent.entities.conversation`) so it can be exercised by focused
unit tests. A caller with access to that tree (server-side, where
:class:`omnigent.stores.conversation_store.ConversationStore` lives) is
expected to walk a conversation's ancestor chain into the ordered
``ancestor_agents`` sequence these functions take, oldest ancestor first,
the same way :func:`omnigent.tools.builtins.spawn._agent_title_from_conversation`
already recovers a single child's agent name from its stored
``"<agent>:<title>"`` conversation title.

Nothing currently calls this module in production — see the port's PR
description for what current enforcement exists instead
(``spawn_bounds`` / ``headless_subagent_purpose_guard`` in
``omnigent/policies/builtins/orchestration.py``, and the direct-child-only
tree scoping in ``omnigent/tools/builtins/spawn.py``) and why none of it
covers delegation depth or reverse-dispatch cycles.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Max delegation chain length considered safe. Matches the historical
#: budget (Hermes -> Omnigent -> Polly -> worker == depth 3) with no
#: assumption about which layer originates the chain.
MAX_DELEGATION_DEPTH = 3


def is_depth_safe(depth: int, *, max_depth: int = MAX_DELEGATION_DEPTH) -> bool:
    """
    Check whether a delegation chain of length *depth* is within bounds.

    :param depth: Number of hops from the chain's root to the dispatch
        being considered, e.g. ``3`` for Hermes -> Omnigent -> Polly ->
        worker.
    :param max_depth: Maximum safe depth, e.g. :data:`MAX_DELEGATION_DEPTH`.
    :returns: ``True`` if ``depth <= max_depth``.
    """
    return depth <= max_depth


def format_depth_error(depth: int, *, max_depth: int = MAX_DELEGATION_DEPTH) -> str:
    """
    Human-readable refusal message for a depth violation.

    :param depth: The offending chain length.
    :param max_depth: The configured limit it exceeded.
    :returns: A message suitable for surfacing back to the LLM/human as the
        reason a dispatch was refused.
    """
    return (
        f"Delegation depth exceeded: {depth} > {max_depth}. This task chain "
        "is too deep to safely extend; stop dispatching further sub-agents "
        "and report back instead."
    )


def detects_reverse_dispatch(ancestor_agents: Sequence[str], target_agent: str) -> bool:
    """
    Check whether dispatching to *target_agent* would close a loop.

    A dispatch is a reverse/recursive loop when the agent it targets is
    already one of the current task's own ancestors — the dispatch would
    hand the task back to something upstream of it rather than delegating
    forward, e.g. Polly (having been handed a task by Hermes) dispatching
    back to Hermes instead of to a coding worker. Forward dispatch to an
    agent that has never appeared in the chain (including a *fresh* dispatch
    to an agent whose name happens to match a legitimate, unrelated worker)
    is unaffected.

    :param ancestor_agents: Agent names from the chain's root to the
        immediate caller, oldest first, e.g. ``("hermes", "omnigent",
        "polly")``. Does not include *target_agent* itself.
    :param target_agent: Name of the sub-agent the caller wants to dispatch
        to next, e.g. ``"hermes"``.
    :returns: ``True`` if *target_agent* already appears in
        *ancestor_agents*.
    """
    return target_agent in ancestor_agents


def format_reverse_dispatch_error(target_agent: str, ancestor_agents: Sequence[str]) -> str:
    """
    Human-readable refusal message for a reverse-dispatch attempt.

    :param target_agent: The sub-agent the caller tried to dispatch to.
    :param ancestor_agents: The chain that already contains *target_agent*,
        oldest first (see :func:`detects_reverse_dispatch`).
    :returns: A message suitable for surfacing back to the LLM/human as the
        reason a dispatch was refused.
    """
    chain = " -> ".join((*ancestor_agents, target_agent))
    return (
        f"Refusing to dispatch to {target_agent!r}: it already appears "
        f"earlier in this task's own delegation chain ({chain}). Honoring "
        "this dispatch would close a reverse/recursive loop back to an "
        "orchestrator upstream of the current task instead of delegating "
        "forward."
    )
