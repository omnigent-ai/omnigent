"""E2E backward-compat guard: a runner that emits ``session.status: waiting``
must not 500 an older server.

When a parent turn ends while a dispatched sub-agent is still running, the
runner publishes ``session.status = "waiting"``. Servers older than 0.3.0 model
``SessionResponse.status`` as ``Literal["idle", "running", "failed"]`` — there
is no ``"waiting"`` — so a naive emit raises a Pydantic ``ValidationError`` and
a 500 on ``GET /v1/sessions/{id}``. The runner's ``/api/version`` gate downgrades
``"waiting"`` -> ``"running"`` against such servers so the response stays
serializable.

This test guards that downgrade end to end: it dispatches a sub-agent (forcing
the parent into the waiting state) and asserts the session stays queryable
(HTTP 200, never 500) across a sustained poll window. It deliberately does NOT
assert the sub-agent result surfaces — auto-wake is a server-side feature older
servers lack, and against such a server the parent sits in the waiting state
the whole time, which is exactly when the un-downgraded 500 would fire. Only the
no-500 runner-side guarantee is checked, and that must hold on EVERY server.

This is the regression guard for the runner waiting-status fix, so unlike the
sub-agent auto-wake tests it is **intentionally NOT** ``min_server_version``-
marked: it must run against pre-0.3.0 servers (that is the whole point). It
fails against such a server when the runner does not downgrade (the 500), and
passes once the runner gates the status — while remaining a trivial pass on a
current server (which serializes ``"waiting"`` natively).

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_waiting_status_compat_e2e.py -v
"""

from __future__ import annotations

import json
import time
import uuid

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

# Statuses a GET may legitimately return. The guard is the HTTP 200 (no
# serialization 500), not the specific status value. In practice GET never
# returns "waiting": a current server collapses cached "waiting"->"running" when
# building the response (server ``_session_status_from_cache``), and the runner
# downgrades "waiting"->"running" for old servers before publishing. "waiting"
# is kept here only defensively — a server that serialized it natively would
# also be a valid 200.
_SERIALIZABLE_STATUSES = {"idle", "running", "failed", "waiting"}


def _sys_session_send_tool_call(agent: str, title: str, child_args: str) -> dict:
    """Build a ``sys_session_send`` tool_calls entry for the mock LLM queue.

    :param agent: Sub-agent tool name to dispatch, e.g. ``"researcher"``.
    :param title: Child session title, e.g. ``"probe"``.
    :param child_args: Free-text task handed to the child, e.g. ``"Investigate"``.
    :returns: A mock-LLM response dict with a single ``sys_session_send`` call.
    """
    return {
        "call_id": "call_1",
        "name": "sys_session_send",
        "arguments": json.dumps({"agent": agent, "title": title, "args": child_args}),
    }


def test_runner_does_not_500_old_server_emitting_waiting_status(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Dispatching a sub-agent forces ``session.status: waiting`` at parent
    turn-end; the session must stay queryable (HTTP 200, never 500) — proving
    the runner downgrades a status the server cannot serialize.

    Does not wait for the sub-agent result (auto-wake is server-gated); it only
    asserts no 500 occurs across a sustained poll of the waiting window. Against
    a pre-0.3.0 server the parent sits in ``waiting`` the whole window, so a
    runner that fails to downgrade would 500 here repeatedly.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Registered runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM base URL (without ``/v1``).
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-waitcompat-parent-{uid}"
    researcher_model = f"mock-waitcompat-researcher-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"

    reset_mock_llm(mock_llm_server_url)

    parent_name = register_inline_agent(
        http_client,
        name=f"waitcompat-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Dispatch the researcher sub-agent via sys_session_send, then stop.",
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "researcher": {
                    "type": "agent",
                    "description": "Test-fixture researcher.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": researcher_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": "You are the test-fixture researcher.",
                },
            },
        },
    )

    # Parent: dispatch the sub-agent, then acknowledge. No auto-wake
    # continuation is queued — the point is the waiting window, not the result.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_sys_session_send_tool_call("researcher", "probe", "Investigate")]},
            {"text": "Dispatched researcher."},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Researcher result."}],
        key=researcher_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the researcher sub-agent.",
    )

    # Poll the parent snapshot across the dispatch + waiting window. Every GET
    # must stay 200 (the regression: a pre-0.3.0 server 500s on an un-downgraded
    # "waiting"). The parent runs its dispatch turn, then — once the sub-agent is
    # live and its turn ends — enters "waiting"; against an un-fixed old server
    # that is sustained (auto-wake never lands), so any GET in the window 500s.
    # Poll the full window (no early break) so that sustained 500 is reliably hit.
    # Separately confirm the sub-agent actually dispatched (a child session
    # exists): otherwise an all-200 result is vacuous — a silently-failed dispatch
    # leaves the parent idle, never entering "waiting", and the test would pass
    # without ever exercising the regression it guards.
    deadline = time.monotonic() + 30.0
    dispatched = False
    polls = 0
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{session_id}")
        assert resp.status_code == 200, (
            f"GET /v1/sessions/{session_id} returned {resp.status_code} (expected 200) — "
            f"the server could not serialize the runner's session status. A pre-0.3.0 "
            f"server 500s on an un-downgraded 'waiting'; the runner must downgrade it. "
            f"Body: {resp.text[:300]}"
        )
        status = resp.json().get("status")
        assert status in _SERIALIZABLE_STATUSES, f"unexpected session status {status!r}"
        polls += 1
        children = http_client.get(f"/v1/sessions/{session_id}/child_sessions")
        assert children.status_code == 200, (
            f"GET child_sessions returned {children.status_code}: {children.text[:200]}"
        )
        if children.json().get("data"):
            dispatched = True
        time.sleep(1.0)

    assert polls >= 5, f"expected a sustained poll of the waiting window; got {polls} polls"
    assert dispatched, (
        "sub-agent was never dispatched (no child session) during the window — the "
        "parent never entered 'waiting', so the all-200 result would be vacuous. "
        "Check the agent / mock-LLM wiring."
    )
