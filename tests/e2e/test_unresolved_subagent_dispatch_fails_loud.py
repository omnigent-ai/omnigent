"""An unresolved sub-agent name must fail loud, never clone the parent.

An unresolved sub-agent name must be a *hard error* on dispatch, not a
silent substitution of the parent spec. On the buggy build the runner's
spec-swap sites resolve the child's ``sub_agent_name`` against the parent
spec tree, and on a miss they keep the already-resolved PARENT spec, so the
child runs with the parent's prompt/tools/harness/model and then reports a
*successful completion*. An orchestrator that fans out five "sub-agents"
receives five substituted parent-clones all claiming success.

Two dispatch paths can reach that fallback; this file pins both:

* ``test_external_subagent_start_undeclared_child_fails_loud`` -- the
  claude-native Task-tool path. Claude Code spawns its own sub-agents and
  the forwarder registers them via ``POST /events`` ``external_subagent_start``,
  which mints a ``kind="sub_agent"`` child row stamping ``sub_agent_name``
  from ``agent_type`` verbatim (e.g. ``"general-purpose"``). Driving a turn
  on that child must fail loud (``sub_agent_unresolved``), never run on the
  parent's spec.

* ``test_create_route_rejects_undeclared_subagent`` -- the
  ``POST /v1/sessions`` create path. An undeclared ``sub_agent_name`` is
  rejected with 404 before any child row is persisted, so it never reaches
  the runner's spec-swap sites. This test drives the passing journey so the
  gate cannot silently regress.

The reproduction runs entirely on the mock LLM: the parent is an
``openai-agents`` agent declaring NO sub-agents, so if the undeclared child
falls back to the parent spec it hits the parent's mock queue and echoes
the parent-clone marker -- proof the child ran on the parent's spec instead
of failing.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke::

    pytest tests/e2e/test_unresolved_subagent_dispatch_fails_loud.py -v --timeout=300
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

# The parent-spec marker. It can only surface in the CHILD's output if the
# child turn ran on the parent's spec (parent model -> parent mock queue).
_PARENT_CLONE_MARKER = "PARENT_CLONE_FALLBACK_SENTINEL"

# An undeclared sub-agent name (claude-native's default Task agent type).
# The parent declares no sub-agents at all, so this never resolves in the
# parent spec tree.
_UNDECLARED_SUB_AGENT = "general-purpose"

pytestmark = [pytest.mark.timeout(600, method="signal")]


def _register_parent_without_subagents(
    http_client: httpx.Client,
    *,
    name: str,
    model: str,
    mock_base_url: str,
) -> str:
    """Register an ``openai-agents`` parent agent that declares NO sub-agents.

    Its mock queue always echoes :data:`_PARENT_CLONE_MARKER`, so a turn that
    resolves to this spec is detectable in the output.

    :param http_client: HTTP client pointed at the live server.
    :param name: Agent display name.
    :param model: Mock model key (also the mock queue key).
    :param mock_base_url: Mock LLM base URL including ``/v1``.
    :returns: The registered agent name (may differ from *name* on rerun).
    """
    return register_inline_agent(
        http_client,
        name=name,
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are the E2E test fixture PARENT orchestrator. You "
            "declare no sub-agents. Always answer with the literal string "
            f"{_PARENT_CLONE_MARKER}."
        ),
        mock_llm_base_url=mock_base_url,
    )


def test_external_subagent_start_undeclared_child_fails_loud(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """The claude-native Task-tool path must not clone the parent silently.

    Journey:

    1. Register a parent agent whose spec declares NO ``general-purpose``
       sub-agent, and start a runner-bound session on it.
    2. Register a sub-agent spawn named ``general-purpose`` through the
       claude-native forwarder route (``external_subagent_start``), which
       mints the child row carrying that undeclared name verbatim.
    3. Drive a turn on the minted child session.
    4. Observe the outcome.

    Contract: an unresolved sub-agent name is a hard error -- the child turn
    fails loud (``sub_agent_unresolved``) and never runs on the parent's
    spec. On the buggy build the child turn instead COMPLETES, echoing the
    parent's marker, so both assertions below fail.
    """
    mock_base = f"{mock_llm_server_url}/v1"
    uid = uuid.uuid4().hex[:8]
    parent_model = f"mock-undeclared-sub-parent-{uid}"

    reset_mock_llm(mock_llm_server_url)
    # If the child falls back to the parent spec, its turn draws from this
    # parent queue. Several copies absorb any harness retry/continuation.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _PARENT_CLONE_MARKER} for _ in range(4)],
        key=parent_model,
    )

    parent_name = _register_parent_without_subagents(
        http_client,
        name=f"undeclared-sub-parent-{uid}",
        model=parent_model,
        mock_base_url=mock_base,
    )
    parent_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )

    # The claude-native forwarder path: Claude Code's Task tool spawned a
    # 'general-purpose' sub-agent; the forwarder registers it here. The
    # server mints a kind="sub_agent" child stamping sub_agent_name from
    # agent_type verbatim.
    resp = http_client.post(
        f"/v1/sessions/{parent_id}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": f"sa_{uid}",
                "agent_type": _UNDECLARED_SUB_AGENT,
                "description": "Undeclared sub-agent dispatched by the parent.",
                "tool_use_id": f"toolu_{uid}",
            },
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    assert resp.status_code in (200, 202), (
        f"external_subagent_start failed: {resp.status_code} {resp.text[:500]}"
    )
    child_id = resp.json()["child_session_id"]

    # The child persisted with the undeclared name -- this is what reaches
    # the runner's spec-swap sites.
    child_snapshot = http_client.get(f"/v1/sessions/{child_id}")
    child_snapshot.raise_for_status()
    assert child_snapshot.json().get("sub_agent_name") == _UNDECLARED_SUB_AGENT

    # Drive a real turn on the undeclared child. On the buggy build the
    # runner keeps the parent spec on the lookup miss and runs the child on
    # the parent's openai-agents spec.
    response_id = send_user_message_to_session(
        http_client,
        session_id=child_id,
        content="Do your task and report the result.",
    )
    result = poll_session_until_terminal(
        http_client,
        session_id=child_id,
        response_id=response_id,
        timeout=240,
    )

    output_blob = str(result.get("output"))

    # Core assertion: the child must NOT have run on the parent's spec.
    assert _PARENT_CLONE_MARKER not in output_blob, (
        f"the undeclared sub-agent {_UNDECLARED_SUB_AGENT!r} ran on the PARENT's "
        f"spec (parent-clone marker {_PARENT_CLONE_MARKER!r} present in the "
        f"child's output) instead of failing loud. "
        f"status={result.get('status')!r} output={result.get('output')!r}"
    )

    # Contract assertion: an unresolved sub-agent dispatch is terminal
    # failure, not a successful completion the orchestrator would trust.
    assert result.get("status") == "failed", (
        f"dispatching undeclared sub-agent {_UNDECLARED_SUB_AGENT!r} COMPLETED "
        f"(status={result.get('status')!r}) instead of surfacing an explicit "
        f"sub_agent_unresolved failure. "
        f"output={result.get('output')!r} error={result.get('error')!r}"
    )


def test_create_route_rejects_undeclared_subagent(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """The ``POST /v1/sessions`` create path rejects an undeclared name.

    A ``sub_agent_name`` the parent's spec does not declare is rejected with
    404 before any child row is persisted, so it never reaches the runner's
    parent-spec swap sites. This pins the passing journey so the gate cannot
    silently regress.
    """
    mock_base = f"{mock_llm_server_url}/v1"
    uid = uuid.uuid4().hex[:8]
    parent_model = f"mock-undeclared-sub-gate-{uid}"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _PARENT_CLONE_MARKER}],
        key=parent_model,
    )

    parent_name = _register_parent_without_subagents(
        http_client,
        name=f"undeclared-sub-gate-{uid}",
        model=parent_model,
        mock_base_url=mock_base,
    )
    parent_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    snapshot = http_client.get(f"/v1/sessions/{parent_id}")
    snapshot.raise_for_status()
    agent_id = snapshot.json()["agent_id"]

    # Dispatch a child by an undeclared sub-agent name through the create
    # route. The gate must reject it up front.
    resp = http_client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_id,
            "sub_agent_name": _UNDECLARED_SUB_AGENT,
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    assert resp.status_code == 404, (
        f"dispatching undeclared sub-agent {_UNDECLARED_SUB_AGENT!r} through "
        f"the create route should be rejected 404, "
        f"got {resp.status_code!r}: {resp.text[:500]}"
    )
    assert _UNDECLARED_SUB_AGENT in resp.text, (
        f"404 body should name the undeclared sub-agent; got {resp.text[:500]}"
    )
