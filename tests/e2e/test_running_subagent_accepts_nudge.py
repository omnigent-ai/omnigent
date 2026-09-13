"""E2E regression guard: a running sub-agent accepts a follow-up nudge.

Field journey this guards (post-completion compaction spiral): an orchestrator
dispatches a coding sub-agent; the sub-agent finishes the hard part (a merge
commit), then an oversized merge-diff enumeration re-fills its context and it
churns without yielding. The parent tries to nudge it ("the merge is DONE —
only prettier+push remains"), but the send is refused with "already has a
launching or running turn", so the parent's only remaining lever is cancelling
the child and finishing the work itself.

What this test pins is the deterministic, omnigent-owned half of that journey:
while a child sub-agent's turn is in-flight (held open on the mock LLM's
blocking gate), the parent's follow-up ``sys_session_send`` to the same child
must NOT be refused outright — it is delivered as a tracked continuation (the
server injects it into the active turn, or starts a fresh one, and either way
the result stays tracked). This test therefore asserts only that the refusal is
gone; it does NOT prove a turn wedged inside a single never-returning LLM call
consumes the message (an injected message is drained only at a turn/step
boundary such a turn never reaches — ``sys_cancel_task`` remains the lever for
a genuinely wedged turn).

Always uses mock-LLM mode (no real LLM needed). Excluded from default
``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_running_subagent_accepts_nudge.py -v
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
)

# httpx polls only — the signal timeout method is safe here (no pexpect/pty).
pytestmark = pytest.mark.timeout(300, method="signal")

# The runner's refusal for a child with an in-flight turn
# (omnigent/runner/tool_dispatch.py). Its presence in the nudge's tool
# output is exactly the reported uninterruptibility.
_REFUSAL_MARKER = "already has a launching or running turn"


def _send_tool_call(call_id: str, agent: str, title: str, args: str) -> dict:
    """Build a ``sys_session_send`` tool_calls entry for the mock LLM queue.

    :param call_id: Unique tool-call id, e.g. ``"call_1"``.
    :param agent: Sub-agent tool name to dispatch, e.g. ``"coder"``.
    :param title: Child session title (reusing it continues the same child).
    :param args: Free-text task handed to the child.
    :returns: A mock-LLM response tool_calls entry.
    """
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"agent": agent, "title": title, "args": args}),
    }


def _conversation_items(http_client: httpx.Client, session_id: str) -> list[dict]:
    """Fetch conversation items in store order, flattened.

    ``GET /v1/sessions/{id}/items`` may nest type-specific fields under
    ``data``; flatten so ``call_id``/``output`` are top-level either way.

    :param http_client: HTTP client pointed at the live server.
    :param session_id: The parent session id.
    :returns: Flattened item dicts in store order.
    """
    resp = http_client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
    resp.raise_for_status()
    items: list[dict] = []
    for item in resp.json()["data"]:
        data = item.get("data")
        items.append({**item, **data} if isinstance(data, dict) else item)
    return items


def _gate_pending(mock_url: str) -> bool:
    """Return whether a mock-LLM request is currently blocked on a gate.

    :param mock_url: Mock LLM server base URL.
    :returns: True when a gated request is waiting.
    """
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def test_parent_nudge_lands_on_child_with_stuck_running_turn(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    A parent's follow-up nudge to a child whose turn is in flight must be
    delivered as a tracked continuation, not refused outright.

    The child's single LLM call blocks on the mock gate, holding its turn
    open indefinitely — the deterministic stand-in for a turn that churns
    through repeated compactions without yielding. The parent then relays a
    nudge via ``sys_session_send`` to the same child title. Pre-fix the runner
    returned the "already has a launching or running turn" refusal as the
    nudge's tool output, leaving cancellation as the parent's only lever; that
    refusal is the bug this test pins. It asserts only that the refusal is
    gone (the nudge is accepted and delivered) — not that a turn wedged inside
    a single never-returning LLM call consumes the message before it yields.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Registered runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM base URL (without ``/v1``).
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-spiral-parent-{uid}"
    child_model = f"mock-spiral-child-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"

    reset_mock_llm(mock_llm_server_url)

    parent_name = register_inline_agent(
        http_client,
        name=f"spiral-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Dispatch the coder sub-agent, then relay any user nudge to it.",
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "coder": {
                    "type": "agent",
                    "description": "Test-fixture coding sub-agent.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": "You are the test-fixture coder.",
                },
            },
        },
    )

    # Parent turn 1: dispatch the coder. Parent turn 2: relay the user's
    # steering nudge to the SAME child title (continuation semantics).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _send_tool_call(
                        "call_1",
                        "coder",
                        "merge-task",
                        "Resolve the merge conflicts, commit, run prettier, push.",
                    )
                ]
            },
            {"text": "Dispatched coder on merge-task."},
            {
                "tool_calls": [
                    _send_tool_call(
                        "call_2",
                        "coder",
                        "merge-task",
                        "The merge commit is DONE. Only prettier on the resolved "
                        "files + push remains; do exactly that and stop.",
                    )
                ]
            },
            {"text": "Nudge relayed."},
        ],
        key=parent_model,
    )
    # Child: its one LLM call blocks on the mock gate, so its turn stays
    # running indefinitely — the deterministic stand-in for a turn stuck
    # re-compacting with no forward progress and no yield.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "merge landed; enumerating changed files...", "block": True}],
        key=child_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    try:
        send_user_message_to_session(
            http_client,
            session_id=session_id,
            content="Resolve the merge on the feature branch via the coder sub-agent.",
        )

        # Wait until the child's turn is genuinely in-flight: its LLM request
        # is blocked on the gate. Without this the nudge could race a
        # not-yet-launched child and pass vacuously.
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if _gate_pending(mock_llm_server_url):
                break
            time.sleep(1.0)
        else:
            pytest.fail(
                "child sub-agent turn never reached the mock LLM gate — the "
                "dispatch failed, so the nudge path was never exercised. "
                "Check the agent / mock-LLM wiring."
            )

        # Wait for the parent's dispatch turn to end (idle or waiting) so the
        # nudge is a fresh turn rather than a mid-turn steer.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            resp = http_client.get(f"/v1/sessions/{session_id}")
            resp.raise_for_status()
            if resp.json().get("status") in ("idle", "waiting"):
                break
            time.sleep(1.0)

        # Turn 2 — the parent relays the steering nudge to the child whose
        # turn is still running (gate still held).
        send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=("Tell the coder: the merge commit is DONE, only prettier+push remains."),
        )

        # Collect the nudge's tool result (call_2's function_call_output).
        nudge_output: str | None = None
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            for item in _conversation_items(http_client, session_id):
                if item.get("type") == "function_call_output" and item.get("call_id") == "call_2":
                    nudge_output = str(item.get("output"))
                    break
            if nudge_output is not None:
                break
            time.sleep(1.0)

        assert nudge_output is not None, (
            "the parent's nudge turn never produced a sys_session_send tool "
            "output (call_2) — the relay turn did not run; cannot judge the "
            "nudge path."
        )

        # THE regression assertion. Pre-fix the runner refused the nudge
        # because the child "already has a launching or running turn",
        # leaving the parent no lever but cancel. The fix steers the nudge
        # into the child's in-flight turn instead, so the refusal must be
        # absent and the tool returns a "running" steer handle. (This does not
        # assert the message is consumed mid-turn — a turn wedged inside a
        # single never-returning LLM call only drains it on yield.)
        assert _REFUSAL_MARKER not in nudge_output, (
            "parent nudge was refused while the child's turn was in-flight: "
            f"{nudge_output!r}. A send to a running child must be steered into "
            "its in-flight turn, not bounced with the launching/running-turn "
            "refusal that leaves sys_cancel_task as the only lever."
        )
        try:
            nudge_handle = json.loads(nudge_output)
        except json.JSONDecodeError:
            nudge_handle = None
        assert nudge_handle is not None and nudge_handle.get("status") == "running", (
            "the accepted nudge must return a 'running' steer handle for the "
            f"in-flight turn, got {nudge_output!r}."
        )
    finally:
        # Unblock the child so fixtures tear down cleanly even on failure.
        for _ in range(5):
            try:
                if not _gate_pending(mock_llm_server_url):
                    break
                release_mock_gate(mock_llm_server_url)
            except httpx.HTTPError:
                break
            time.sleep(0.5)
