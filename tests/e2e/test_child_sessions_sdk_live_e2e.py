"""Exercise the sub-agent SDK helpers against a LIVE server, deterministically.

No LLM required — creates real child / grandchild sub-agent sessions via
``parent_session_id`` on ``POST /v1/sessions`` (the route stamps
``kind="sub_agent"``), then drives the client helpers against the real
``GET /v1/sessions/{id}/child_sessions`` endpoint:

* ``SessionsNamespace.child_sessions``       — one level
* ``SessionsNamespace.child_sessions_tree``  — recursive BFS + parent tagging
* ``SessionsNamespace.subtree_busy``         — the "is anything still working
  in this subtree?" rollup an SDK driver gates "your turn" on (issue #444)

The keyless, CI-runnable mirror of the mocked unit tests in
``tests/frontends/sdk/test_sessions_namespace.py`` and the real-LLM archer e2e
in ``test_repl_subagent_panel_events_e2e.py`` (skipped without
``--llm-api-key``): it pins the real endpoint/SDK contract that feeds the CLI
``↓`` sub-agent selector + ``state: N agents running`` badge in the default
(no-key) e2e lane, where the LLM-gated test cannot run.
"""

from __future__ import annotations

import asyncio

import httpx
from omnigent_client._sessions import SessionsNamespace

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import create_runner_bound_session, lookup_agent_id


def _create_child(http_client: httpx.Client, *, agent_id: str, parent_id: str) -> str:
    """Create a sub-agent child session under *parent_id* and return its id."""
    resp = http_client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "parent_session_id": parent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def test_child_sessions_sdk_helpers_against_live_server(
    live_server: str,
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """The CLI/SDK sub-agent helpers read the real endpoint correctly."""
    agent_id = lookup_agent_id(http_client, archer_agent)
    parent = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    # Two direct children + one grandchild under the first child.
    child_a = _create_child(http_client, agent_id=agent_id, parent_id=parent)
    child_b = _create_child(http_client, agent_id=agent_id, parent_id=parent)
    grand = _create_child(http_client, agent_id=agent_id, parent_id=child_a)

    # ── Raw endpoint: lists the two direct children with the documented shape.
    resp = http_client.get(f"/v1/sessions/{parent}/child_sessions")
    resp.raise_for_status()
    rows = resp.json()["data"]
    ids = {r["id"] for r in rows}
    assert ids == {child_a, child_b}, f"direct children mismatch: {ids}"
    for r in rows:
        assert "busy" in r and "current_task_status" in r, (
            f"row missing badge fields: keys={sorted(r)}"
        )
        assert r["parent_session_id"] == parent
        assert r["kind"] == "sub_agent"

    async def _drive() -> dict[str, object]:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            ns = SessionsNamespace(ac, live_server)
            one_level = await ns.child_sessions(parent)
            tree = await ns.child_sessions_tree(parent)
            busy = await ns.subtree_busy(parent)
            # A child with no parent of its own (grandchild's parent) — recursion
            # must descend into child_a to find the grandchild.
            return {"one_level": one_level, "tree": tree, "busy": busy}

    out = asyncio.run(_drive())

    # ── child_sessions: one level only (no grandchild).
    one_ids = {r["id"] for r in out["one_level"]}  # type: ignore[union-attr]
    assert one_ids == {child_a, child_b}, f"child_sessions one-level mismatch: {one_ids}"

    # ── child_sessions_tree: recurses to the grandchild + tags parent_id.
    tree = out["tree"]  # type: ignore[assignment]
    by_id = {n["id"]: n for n in tree}  # type: ignore[union-attr]
    assert set(by_id) == {child_a, child_b, grand}, f"tree ids mismatch: {set(by_id)}"
    assert by_id[child_a]["parent_id"] == parent
    assert by_id[child_b]["parent_id"] == parent
    assert by_id[grand]["parent_id"] == child_a, (
        f"grandchild mis-tagged: {by_id[grand].get('parent_id')}"
    )

    # ── subtree_busy: all children are freshly-created with no task → idle →
    # the rollup must read False ("safe to inject your turn"). This is the
    # deterministic half of issue #444's contract.
    assert out["busy"] is False, "subtree_busy should be False when no child has a task"
