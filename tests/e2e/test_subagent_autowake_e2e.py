"""End-to-end test for sub-agent auto-wake (mock LLM).

When a sub-agent finishes, the runner delivers its result to the parent's
inbox AND posts a ``[System: ... waiting in inbox]`` wake notice to the
parent's event stream, so an idle orchestrator takes a continuation turn and
surfaces the result -- without the user sending another message.

The wake notice substring ``waiting in inbox`` is produced ONLY by the
auto-wake path (``_format_subagent_wake_notice``); it is distinct from the
``sys_read_inbox`` drain message. So its presence is an auto-wake-specific
signal.

All tests use mock-LLM keyed queues. The parent agent is registered via
``register_inline_agent`` with ``mock_llm_base_url`` so the parent harness
always calls the mock server. The inline researcher sub-agent spec carries
``auth.base_url`` pointing at the mock server — now propagated through
``_agent_tool_to_sub_spec`` via the ``raw_executor`` parameter so child
sub-agents never reach the real LLM API even under CI's
``--llm-api-key`` / ``--profile`` mode.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke
with::

    pytest tests/e2e/test_subagent_autowake_e2e.py -v --timeout=60
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
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# The auto-wake notice is the ONLY place this substring is emitted.
_WAKE_NOTICE_SIGNATURE = "waiting in inbox"
_RESEARCHER_MARKER = "RESEARCHER_MARKER_2025"

# Each test is 3+ serial gateway turns, so 600s absorbs potential backoff.
pytestmark = pytest.mark.timeout(600, method="signal")


@pytest.fixture(scope="session")
def autowake_test_agent(
    http_client: httpx.Client,
    mock_llm_server_url: str,
) -> tuple[str, str, str]:
    """Register parent + researcher with mock LLM URLs.

    Returns ``(parent_name, parent_model, researcher_model)``.

    ``mock_llm_base_url`` bakes the mock server URL directly into each
    executor's ``auth.base_url``. The child researcher's auth propagates
    through ``_agent_tool_to_sub_spec`` (``raw_executor`` fix) so it
    never reaches the real LLM API.

    :param http_client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: Tuple ``(parent_name, parent_model, researcher_model)``.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-aw-parent-{uid}"
    researcher_model = f"mock-aw-researcher-{uid}"

    # openai-agents harness expects /v1 in the base URL.
    mock_base = f"{mock_llm_server_url}/v1"

    parent_name = register_inline_agent(
        http_client,
        name=f"aw-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are the auto-wake E2E test fixture parent. Dispatch the "
            "researcher sub-agent via sys_session_send and report the "
            "literal marker string it returns."
        ),
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "researcher": {
                    "type": "agent",
                    "description": "Test-fixture researcher. Returns RESEARCHER_MARKER_2025.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": researcher_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": (
                        "You are the test-fixture researcher. Include "
                        "RESEARCHER_MARKER_2025 verbatim in your response."
                    ),
                },
            },
        },
    )
    return parent_name, parent_model, researcher_model


# ─── Mock helpers ────────────────────────────────────────────


def _sys_session_send_tool_call(
    agent: str,
    title: str,
    child_args: str,
    *,
    call_id: str = "call_1",
) -> dict:
    """Build a tool_calls response entry for ``sys_session_send``."""
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"agent": agent, "title": title, "args": child_args}),
    }


def _session_items_blob(http_client: httpx.Client, session_id: str) -> str:
    """Return all items in a session snapshot as one JSON string."""
    resp = http_client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return json.dumps(resp.json().get("items", []))


def _count_wake_notices(http_client: httpx.Client, session_id: str) -> int:
    """Count auto-wake notices in a session snapshot."""
    return _session_items_blob(http_client, session_id).count(_WAKE_NOTICE_SIGNATURE)


def _configure_dispatch_flow(
    mock_url: str,
    *,
    parent_model: str,
    researcher_model: str,
    title: str = "auth",
    child_args: str = "Research auth patterns",
) -> None:
    """Configure mock queues for a single dispatch-and-autowake flow.

    Parent queue (keyed by parent_model):
    1. dispatch: sys_session_send tool call.
    2. after tool result: text acknowledging dispatch.
    3. auto-wake continuation: text quoting the marker.

    Researcher queue (keyed by researcher_model):
    1. child turn: text containing the marker.
    """
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    _sys_session_send_tool_call("researcher", title, child_args),
                ],
            },
            {"text": "Dispatched researcher, waiting for result."},
            # Parent auto-wake continuation.
            {"text": f"The researcher returned: {_RESEARCHER_MARKER}"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_url,
        [{"text": f"Research complete. {_RESEARCHER_MARKER}"}],
        key=researcher_model,
    )


# ─── Tests ───────────────────────────────────────────────────


def test_subagent_completion_auto_wakes_idle_parent(
    http_client: httpx.Client,
    live_runner_id: str,
    autowake_test_agent: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """Dispatching a sub-agent then sending nothing else still surfaces its
    result, because the parent is auto-woken when the sub-agent completes.

    Flow:
    1. One user message tells the parent to dispatch the researcher.
    2. The dispatch turn goes terminal (sub-agent runs async).
    3. With NO further user input, the sub-agent completes, the runner
       posts the wake notice, and the parent takes a continuation turn.
    """
    parent_name, parent_model, researcher_model = autowake_test_agent
    reset_mock_llm(mock_llm_server_url)
    _configure_dispatch_flow(
        mock_llm_server_url,
        parent_model=parent_model,
        researcher_model=researcher_model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    dispatch_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the researcher sub-agent.",
    )

    # Dispatch turn goes terminal on its own.
    poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=dispatch_response_id,
        timeout=180,
    )

    # From here we send NOTHING. The wake notice and marker can only
    # appear via the auto-wake continuation turn.
    deadline = time.monotonic() + 240
    wake_seen = False
    marker_seen = False
    while time.monotonic() < deadline:
        blob = _session_items_blob(http_client, session_id)
        wake_seen = wake_seen or _WAKE_NOTICE_SIGNATURE in blob
        marker_seen = _RESEARCHER_MARKER in blob
        if wake_seen and marker_seen:
            break
        time.sleep(POLL_INTERVAL_S)

    assert wake_seen, (
        f"No auto-wake notice ({_WAKE_NOTICE_SIGNATURE!r}) appeared in session "
        f"{session_id} after the dispatch turn ended."
    )
    assert marker_seen, (
        f"Researcher marker {_RESEARCHER_MARKER!r} never surfaced in session {session_id}."
    )


def test_subagent_completion_auto_wakes_parent_on_a_second_round(
    http_client: httpx.Client,
    live_runner_id: str,
    autowake_test_agent: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """Re-dispatching the SAME sub-agent in a second round wakes the parent again.

    Coarse CUJ for the multi-round auto-wake path: round 1 dispatches and
    the parent is auto-woken; round 2 re-dispatches and the parent must be
    auto-woken AGAIN, asserted by the wake-notice count strictly increasing.
    """
    parent_name, parent_model, researcher_model = autowake_test_agent
    reset_mock_llm(mock_llm_server_url)

    # Queue responses for round 1 AND round 2 on the parent model queue.
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Round 1: dispatch
            {
                "tool_calls": [
                    _sys_session_send_tool_call("researcher", "round1", "Research round 1"),
                ],
            },
            {"text": "Dispatched, waiting."},
            # Round 1: parent auto-wake continuation
            {"text": f"Round 1 result: {_RESEARCHER_MARKER}"},
            # Round 2: dispatch
            {
                "tool_calls": [
                    _sys_session_send_tool_call("researcher", "round2", "Research round 2"),
                ],
            },
            {"text": "Re-dispatched, waiting."},
            # Round 2: parent auto-wake continuation
            {"text": f"Round 2 result: {_RESEARCHER_MARKER}"},
        ],
        key=parent_model,
    )
    # Child responses for round 1 and 2 on the researcher model queue.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": f"Round 1 done. {_RESEARCHER_MARKER}"},
            {"text": f"Round 2 done. {_RESEARCHER_MARKER}"},
        ],
        key=researcher_model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )

    # ── Round 1 ──
    round1_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the researcher sub-agent.",
    )
    poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=round1_response_id,
        timeout=180,
    )

    deadline = time.monotonic() + 240
    round1_wakes = 0
    marker_seen = False
    while time.monotonic() < deadline:
        round1_wakes = _count_wake_notices(http_client, session_id)
        marker_seen = _RESEARCHER_MARKER in _session_items_blob(http_client, session_id)
        if round1_wakes >= 1 and marker_seen:
            break
        time.sleep(POLL_INTERVAL_S)

    assert round1_wakes >= 1 and marker_seen, (
        f"Round 1 did not auto-wake the parent in session {session_id} "
        f"(wakes={round1_wakes}, marker_seen={marker_seen})."
    )

    # ── Round 2: re-dispatch ──
    round2_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the researcher sub-agent again.",
    )
    poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=round2_response_id,
        timeout=180,
    )

    deadline = time.monotonic() + 240
    round2_wakes = round1_wakes
    while time.monotonic() < deadline:
        round2_wakes = _count_wake_notices(http_client, session_id)
        if round2_wakes > round1_wakes:
            break
        time.sleep(POLL_INTERVAL_S)

    assert round2_wakes > round1_wakes, (
        f"No NEW auto-wake notice for round 2 in session {session_id} "
        f"(round1_wakes={round1_wakes}, round2_wakes={round2_wakes})."
    )
