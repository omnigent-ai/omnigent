"""Fan-out model advisor for sys_advise_models.

Provides :func:`advise_fanout`, which maps a list of orchestrator tasks
to per-task model recommendations using :class:`~omnigent.server.smart_routing.RoutingClient`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

# Heuristic harness mapping when sub-agent spec is not available.
_WORKER_HARNESS: dict[str, str] = {
    "claude_code": "claude-sdk",
    "codex": "codex",
    "pi": "openai-agents",
}


def _harness_for_task(agent: str, spec: Any | None) -> str | None:  # type: ignore[explicit-any]
    """Resolve the harness for a worker agent.

    Prefers the harness declared on the sub-agent spec, then falls
    back to a name-based heuristic.

    :param agent: Sub-agent name, e.g. ``"claude_code"`` or
        ``"researcher"``.
    :param spec: Parent agent's :class:`~omnigent.spec.types.AgentSpec`,
        or ``None`` if unavailable.
    :returns: Harness id, e.g. ``"claude-sdk"``, or ``None`` if
        unresolvable.
    """
    # Try spec sub-agents first.
    if spec is not None:
        sub_agents = getattr(spec, "sub_agents", None) or []
        for sub in sub_agents:
            sub_name = getattr(sub, "name", None)
            if sub_name == agent:
                from omnigent.model_catalog import spec_harness

                harness = spec_harness(sub)
                if harness:
                    return harness
                break  # found the entry but harness was not declared
    # Fall back to heuristic.
    return _WORKER_HARNESS.get(agent)


async def advise_fanout(
    tasks: list[dict[str, str]],
    spec: Any | None,  # type: ignore[explicit-any]
    conversation_id: str | None,  # noqa: ARG001  # reserved for future enforcement
) -> list[dict[str, Any]]:  # type: ignore[explicit-any]
    """Recommend a model for each fan-out task.

    For each task ``{title, agent, task}`` the advisor:

    1. Resolves the worker harness (via spec sub-agents or heuristic).
    2. Calls :func:`~omnigent.server.smart_routing.infer_tiers` to get
       the available tiers for that harness.
    3. Calls ``routing_client.route(task_description, tiers)`` to obtain
       a :class:`~omnigent.server.smart_routing.RoutingResult`.

    :param tasks: List of task dicts with keys ``title``, ``agent``,
        and ``task``.
    :param spec: Parent agent's :class:`~omnigent.spec.types.AgentSpec`
        used to resolve sub-agent harnesses. ``None`` if not available.
    :param conversation_id: Parent conversation id (informational only,
        for future enforcement).
    :returns: List of recommendation dicts, one per input task, each
        with keys ``title``, ``agent``, ``model``, ``tier``, and
        ``rationale``.
    """
    from omnigent.runtime._globals import _caps
    from omnigent.server.smart_routing import infer_tiers

    routing_client = _caps.routing_client

    results: list[dict[str, Any]] = []  # type: ignore[explicit-any]

    for task in tasks:
        title = task.get("title", "")
        agent = task.get("agent", "")
        task_text = task.get("task", "")

        if routing_client is None:
            results.append(
                {
                    "title": title,
                    "agent": agent,
                    "model": None,
                    "tier": None,
                    "rationale": "router not configured",
                }
            )
            continue

        harness = _harness_for_task(agent, spec)
        tiers = infer_tiers(harness) if harness else None

        if tiers is None:
            results.append(
                {
                    "title": title,
                    "agent": agent,
                    "model": None,
                    "tier": None,
                    "rationale": f"no tiers available for harness {harness!r}",
                }
            )
            continue

        try:
            verdict = await routing_client.route(task_text, tiers)
        except Exception:
            _logger.exception(
                "advise_fanout: routing_client.route failed for task %r agent %r",
                title,
                agent,
            )
            verdict = None

        if verdict is None:
            results.append(
                {
                    "title": title,
                    "agent": agent,
                    "model": None,
                    "tier": None,
                    "rationale": "router returned no verdict",
                }
            )
        else:
            results.append(
                {
                    "title": title,
                    "agent": agent,
                    "model": verdict.model,
                    "tier": verdict.tier,
                    "rationale": verdict.rationale,
                }
            )

    return results
