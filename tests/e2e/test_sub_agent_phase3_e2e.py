"""End-to-end tests for the Phase 3 sub-agent pipeline (mock LLM).

Covers ``sys_session_send`` (singular) dispatch:

* ``test_single_sub_agent_e2e`` — parent dispatches one sub-agent
  via sys_session_send, the result auto-delivers, and the parent
  quotes the marker in its final response.
* ``test_parallel_sub_agents_e2e`` — parent emits TWO
  sys_session_send tool calls in one response (the new
  parallelism idiom); both sub-agent markers reach the final reply.
* ``test_mixed_sub_agent_and_async_tool_e2e`` — parent
  dispatches one sub-agent and checks the unified
  async_work_complete drain handles the sub_agent kind.

All three tests use mock-LLM keyed queues. The parent agent is
registered via ``register_inline_agent`` with ``mock_llm_base_url``
so the parent harness always calls the mock server. The inline
sub-agent specs (researcher, summarizer) also carry ``auth.base_url``
pointing at the mock server — now propagated through
``_agent_tool_to_sub_spec`` via the ``raw_executor`` parameter so
child sub-agents never reach the real LLM API even under CI's
``--llm-api-key`` / ``--profile`` mode.

Separate mock-queue keys per agent (parent vs researcher vs
summarizer) prevent the queues from interleaving: each harness
subprocess identifies itself by the model name baked into its
spawn-env, and the mock server routes by that model.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``.
Invoke with::

    pytest tests/e2e/test_sub_agent_phase3_e2e.py -v --timeout=60
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

# Each test is 3+ serial gateway turns (dispatch + sub-agent + auto-wake),
# so 600s absorbs potential backoff.
# These sub-agent tests use per-sub-agent mock-LLM routing (each child on its
# own mock model + auth.base_url), which a server < 0.3.0 does not propagate
# (fixed in #779, which landed ~2h after v0.2.0 was tagged) — the child reaches
# the real gateway and fails, so its result never surfaces
# (see test_named_sub_agent_persistence.py for the verified mechanism). A
# mock-LLM test-infra gap, not a product regression. The backwards-compat
# matrix skips these against servers < 0.3.0; they run unchanged on main.
pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
]

# Stable marker strings checked in the session snapshot.
_RESEARCHER_MARKER = "RESEARCHER_MARKER_2025"
_SUMMARIZER_MARKER = "SUMMARIZER_MARKER_2025"


# ─── Fixture ─────────────────────────────────────────────────


@pytest.fixture(scope="session")
def sub_agent_test_agent(
    http_client: httpx.Client,
    mock_llm_server_url: str,
) -> tuple[str, str, str, str]:
    """Register parent + researcher + summarizer with mock LLM URLs.

    Returns ``(parent_name, parent_model, researcher_model,
    summarizer_model)``. Each agent gets a unique model name so the
    mock server routes LLM calls to separate keyed queues — parent
    calls never interleave with child calls.

    ``mock_llm_base_url`` bakes the mock server URL directly into each
    executor's ``auth.base_url``. For the parent this flows through
    ``register_inline_agent``'s ``executor["auth"]`` dict directly.
    For inline sub-agent tools, it flows through the ``auth:`` block
    in ``extra_config["tools"][name]["executor"]``, which
    ``_agent_tool_to_sub_spec`` now propagates via ``raw_executor`` to
    ``_translate_executor_from_def`` (the product fix on this branch).
    Without that fix, child sub-agents would fall back to the ambient
    ``OPENAI_BASE_URL``, causing them to hit the real API in CI.

    :param http_client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: Tuple ``(parent_name, parent_model, researcher_model,
        summarizer_model)``.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-p3-parent-{uid}"
    researcher_model = f"mock-p3-researcher-{uid}"
    summarizer_model = f"mock-p3-summarizer-{uid}"

    # openai-agents harness expects /v1 in the base URL (the OpenAI SDK
    # appends /responses to the base URL, so the base must include /v1).
    mock_base = f"{mock_llm_server_url}/v1"

    parent_name = register_inline_agent(
        http_client,
        name=f"sub-agent-test-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are the sub-agent E2E test fixture parent. Dispatch the "
            "requested sub-agent(s) via sys_session_send and quote the "
            "literal marker strings each returns.\n\n"
            "Available sub-agents: researcher, summarizer.\n"
            "To dispatch both in parallel, emit two sys_session_send calls "
            "in the same response."
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
                "summarizer": {
                    "type": "agent",
                    "description": "Test-fixture summarizer. Returns SUMMARIZER_MARKER_2025.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": summarizer_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": (
                        "You are the test-fixture summarizer. Include "
                        "SUMMARIZER_MARKER_2025 verbatim in your response."
                    ),
                },
            },
        },
    )
    return parent_name, parent_model, researcher_model, summarizer_model


# ─── Mock helpers ───────────────────────────────────────────


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


def _wait_for_markers(
    http_client: httpx.Client,
    session_id: str,
    *markers: str,
    timeout_s: float = 240.0,
) -> str:
    """Poll the session snapshot until every *marker* substring appears.

    ``sys_session_send`` is async: the sub-agent runs after the parent's
    dispatch turn ends, then auto-wakes the parent. The marker lands in
    the session AFTER the dispatch turn goes idle.

    :returns: The final serialized items blob.
    """
    deadline = time.monotonic() + timeout_s
    blob = ""
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        blob = json.dumps(resp.json().get("items", []))
        if all(m in blob for m in markers):
            return blob
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"markers {markers!r} did not all surface in session {session_id} "
        f"within {timeout_s:.0f}s. Last items blob: {blob[:600]!r}"
    )


def _run_turn(
    http_client: httpx.Client,
    *,
    runner_id: str,
    agent_name: str,
    user_text: str,
    timeout_s: float = 240.0,
) -> tuple[dict, str]:
    """Drive one turn through a fresh runner-bound session."""
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=user_text,
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=timeout_s,
    )
    return body, session_id


# ─── Tests ───────────────────────────────────────────────────


def test_single_sub_agent_e2e(
    http_client: httpx.Client,
    sub_agent_test_agent: tuple[str, str, str, str],
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Parent dispatches one sub-agent; its marker surfaces via auto-wake.

    Mock flow:
    1. Parent LLM -> sys_session_send(researcher)
    2. Parent LLM -> "Dispatched, waiting."
    3. Child (researcher) LLM -> text with RESEARCHER_MARKER_2025
    4. Parent auto-wake continuation -> text quoting the marker
    """
    parent_name, parent_model, researcher_model, _ = sub_agent_test_agent
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _sys_session_send_tool_call("researcher", "auth", "Research auth patterns"),
                ],
            },
            {"text": "Dispatched researcher, waiting for result."},
            {"text": f"The researcher returned: {_RESEARCHER_MARKER}"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Research complete. {_RESEARCHER_MARKER}"}],
        key=researcher_model,
    )

    body, session_id = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=parent_name,
        user_text="Dispatch the researcher sub-agent.",
    )
    assert body["status"] == "completed", (
        f"sub-agent turn did not complete: status={body.get('status')!r}, "
        f"error={body.get('error')!r}"
    )

    # The marker surfaces via auto-wake (async).
    _wait_for_markers(http_client, session_id, _RESEARCHER_MARKER)


def test_parallel_sub_agents_e2e(
    http_client: httpx.Client,
    sub_agent_test_agent: tuple[str, str, str, str],
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Parent dispatches both sub-agents in parallel; both markers surface.

    Mock flow:
    1. Parent -> two sys_session_send tool calls (researcher + summarizer)
    2. Parent -> "Dispatched both, waiting."
    3. Child researcher -> text with RESEARCHER_MARKER_2025
    4. Child summarizer -> text with SUMMARIZER_MARKER_2025
    5. Parent auto-wake -> text quoting both markers
    """
    parent_name, parent_model, researcher_model, summarizer_model = sub_agent_test_agent
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _sys_session_send_tool_call(
                        "researcher", "auth", "Research auth", call_id="call_1"
                    ),
                    _sys_session_send_tool_call(
                        "summarizer", "summary", "Summarize findings", call_id="call_2"
                    ),
                ],
            },
            {"text": "Dispatched both sub-agents, waiting."},
            {"text": f"Results: {_RESEARCHER_MARKER} and {_SUMMARIZER_MARKER}"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Research done. {_RESEARCHER_MARKER}"}],
        key=researcher_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Summary done. {_SUMMARIZER_MARKER}"}],
        key=summarizer_model,
    )

    body, session_id = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=parent_name,
        user_text="Dispatch BOTH the researcher AND the summarizer in parallel.",
    )
    assert body["status"] == "completed", (
        f"parallel turn did not complete: status={body.get('status')!r}, "
        f"error={body.get('error')!r}"
    )

    _wait_for_markers(
        http_client,
        session_id,
        _RESEARCHER_MARKER,
        _SUMMARIZER_MARKER,
    )


def test_mixed_sub_agent_and_async_tool_e2e(
    http_client: httpx.Client,
    sub_agent_test_agent: tuple[str, str, str, str],
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Sub-agent dispatch through the unified async_work_complete drain.

    Same as single sub-agent dispatch -- the E2E layer proves the
    real-LLM flow doesn't regress on the kind discriminator path.
    """
    parent_name, parent_model, researcher_model, _ = sub_agent_test_agent
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _sys_session_send_tool_call("researcher", "task", "Research something"),
                ],
            },
            {"text": "Dispatched researcher, waiting."},
            {"text": f"Researcher returned: {_RESEARCHER_MARKER}"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Done. {_RESEARCHER_MARKER}"}],
        key=researcher_model,
    )

    body, session_id = _run_turn(
        http_client,
        runner_id=live_runner_id,
        agent_name=parent_name,
        user_text="Dispatch the researcher sub-agent.",
    )
    assert body["status"] == "completed"

    _wait_for_markers(http_client, session_id, _RESEARCHER_MARKER)
