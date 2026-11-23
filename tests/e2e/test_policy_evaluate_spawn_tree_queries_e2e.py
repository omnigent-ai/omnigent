"""A policy check on a sub-agent must not get costlier page by page as its spawn tree grows.

A session-wide cost budget is checked on every tool call a sub-agent makes:
Claude Code's ``PreToolUse`` hook posts the call to
``/v1/sessions/{id}/policies/evaluate``, and the server loads the whole spawn
tree to sum its spend against the root's budget. In a large fan-out that check
runs constantly, so reading the tree must not cost another listing for every
100 sessions in it.

Everything runs through a real server process. The root, its budget and its
nested sub-agents are created over the HTTP API, a grandchild's tool call is
evaluated as the hook sends it, and the server's own OpenTelemetry export
reports the database work of each evaluation.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from tests._helpers.live_server import isolated_local_server
from tests._helpers.native_session import create_native_session
from tests._helpers.otlp import OtlpTraceCollector
from tests._helpers.session import create_sub_agent_sessions

EVALUATE_ROUTE_SPAN = "POST /v1/sessions/{session_id}/policies/evaluate"
# The event Claude Code's PreToolUse hook posts before a Bash call.
TOOL_CALL_EVENT = {
    "event": {
        "type": "PHASE_TOOL_CALL",
        "target": "",
        "data": {"name": "Bash", "arguments": {"command": "make test"}},
        "context": {},
    }
}
CHILDREN = 3
# With the evaluated sub-agent, 95 sessions fit one 100-row listing; 215 need three.
GRANDCHILDREN_PER_CHILD_BEFORE = 30
GRANDCHILDREN_PER_CHILD_AFTER = 70


def _evaluate_tool_call(
    client: httpx.Client, collector: OtlpTraceCollector, session_id: str
) -> list[dict[str, Any]]:
    """Evaluate a Bash call on ``session_id`` as the hook does; return its database spans."""
    response, spans = collector.traced_request(
        client,
        "POST",
        f"/v1/sessions/{session_id}/policies/evaluate",
        route_span=EVALUATE_ROUTE_SPAN,
        json=TOOL_CALL_EVENT,
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "POLICY_ACTION_ALLOW", response.json()
    return [span for span in spans if "db.system" in span["attributes"]]


def _steady_evaluation(
    client: httpx.Client, collector: OtlpTraceCollector, session_id: str
) -> list[dict[str, Any]]:
    # The first check after the tree changes can fill caches; measure the next one.
    _evaluate_tool_call(client, collector, session_id)
    return _evaluate_tool_call(client, collector, session_id)


def test_budget_check_cost_on_a_sub_agent_does_not_grow_with_the_tree(tmp_path: Path) -> None:
    with (
        OtlpTraceCollector() as collector,
        isolated_local_server(
            tmp_path / "server",
            # Read the session's grants on every request: the 5 s access cache
            # could expire inside one measured check and not the other.
            env={**collector.exporter_env(), "OMNIGENT_ACL_RESOLVE_CACHE_TTL_S": "0"},
        ) as base_url,
        httpx.Client(base_url=base_url, trust_env=False, timeout=60) as client,
    ):
        root = create_native_session(
            client, base_url, harness="claude", metadata={"title": "budgeted fan-out"}
        )
        root_id, agent_id = root["session_id"], root["agent_id"]
        budget = client.post(
            f"/v1/sessions/{root_id}/policies",
            json={
                "name": "session_cost_budget",
                "type": "python",
                "handler": "omnigent.policies.builtins.cost.cost_budget",
                "factory_params": {"max_cost_usd": 50.0},
            },
        )
        assert budget.status_code < 300, budget.text
        unbudgeted = create_native_session(
            client, base_url, harness="claude", metadata={"title": "no policies"}
        )["session_id"]
        children = create_sub_agent_sessions(
            client, agent_id=agent_id, parent_id=root_id, count=CHILDREN
        )
        for child in children:
            create_sub_agent_sessions(
                client, agent_id=agent_id, parent_id=child, count=GRANDCHILDREN_PER_CHILD_BEFORE
            )
        evaluated = create_sub_agent_sessions(
            client, agent_id=agent_id, parent_id=children[0], count=1
        )[0]

        # A session with no policies takes the evaluate fast path and loads no tree.
        fast_path = _steady_evaluation(client, collector, unbudgeted)
        small_tree = _steady_evaluation(client, collector, evaluated)
        for child in children:
            create_sub_agent_sessions(
                client,
                agent_id=agent_id,
                parent_id=child,
                count=GRANDCHILDREN_PER_CHILD_AFTER - GRANDCHILDREN_PER_CHILD_BEFORE,
            )
        large_tree = _steady_evaluation(client, collector, evaluated)

    small_sessions = 1 + CHILDREN * (1 + GRANDCHILDREN_PER_CHILD_BEFORE) + 1
    large_sessions = 1 + CHILDREN * (1 + GRANDCHILDREN_PER_CHILD_AFTER) + 1
    assert fast_path, "the server exported no database spans for the check; is tracing installed?"
    assert len(small_tree) > len(fast_path), (
        "the budgeted check did no more database work than the policy-less fast path, "
        "so the budget's tree read never ran"
    )
    assert len(large_tree) == len(small_tree), (
        f"checking the budget ran {len(small_tree)} database operations with {small_sessions} "
        f"sessions in the tree and {len(large_tree)} with {large_sessions}: "
        f"{dict(Counter(span['name'].split(' ')[0] for span in small_tree))} -> "
        f"{dict(Counter(span['name'].split(' ')[0] for span in large_tree))}"
    )
