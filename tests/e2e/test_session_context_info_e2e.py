"""E2E: context occupancy and turn recency survive a real relay turn (mock LLM).

No LLM key required — an inline ``openai-agents`` agent runs one turn against
the mock LLM server, then the real ``GET /v1/sessions/{id}`` is read back.

The claim under test is **parity**. The context-fill labels were historically
written only by the ``external_session_usage`` event that terminal-backed
harnesses post, so a relay (SDK) session served ``last_total_tokens: null``
forever while a native one served a real number. An orchestrator mixing both
in one sub-agent tree has to apply a single compaction policy across them, so
"works on native only" is indistinguishable from broken.

The runner-side projection is unit-tested against a mocked transport in
``tests/runner/test_runner_dispatch.py``; what only a live server can prove is
that the fields those tests assume are actually the ones a real snapshot
carries. So this drives ``_session_get_info_via_rest`` against the real
server rather than re-asserting the projection logic.

Usage::

    pytest tests/e2e/test_session_context_info_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)


def _get_info_against(live_server: str, session_id: str) -> dict[str, Any]:
    """
    Run the runner's ``sys_session_get_info`` projection against a live server.

    :param live_server: Base URL of the running server.
    :param session_id: Session to describe.
    :returns: The parsed tool output.
    """
    from omnigent.runner.tool_dispatch import _session_get_info_via_rest

    async def _run() -> str:
        async with httpx.AsyncClient(
            base_url=live_server,
            timeout=60,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as client:
            return await _session_get_info_via_rest(
                {"session_id": session_id},
                session_id,
                client,
            )

    return json.loads(asyncio.run(_run()))


def test_relay_turn_reports_context_fill_and_turn_recency(
    live_server: str,
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A relay session reports its context fill, as a native one always did.

    **What breaks if wrong:**

    - If the relay usage path drops ``context_tokens``, the snapshot serves
      ``last_total_tokens: null`` and every SDK sub-agent looks unmeasured —
      the state this whole feature exists to remove.
    - If the fill timestamp isn't written beside the count, a caller cannot
      tell a fresh reading from one a failed turn left behind.
    - If the runner projection reads field names the server doesn't serve,
      the block comes back all-null against a real server while the mocked
      unit tests still pass.
    """
    model = f"mock-ctx-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"ctx-info-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    configure_mock_llm(mock_llm_server_url, [{"text": "Done."}], key=model)

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Say done.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )
    assert body["status"] == "completed", (
        f"Expected completed, got {body['status']}. Error: {body.get('error')}."
    )

    snapshot = http_client.get(f"/v1/sessions/{session_id}").json()

    # The parity claim: a relay turn's fill reaches the snapshot. This is
    # null on a server without the relay-side persistence.
    tokens = snapshot["last_total_tokens"]
    assert isinstance(tokens, int) and tokens > 0, (
        f"relay turn recorded no context fill: last_total_tokens={tokens!r}"
    )
    # Stamped, so staleness is decidable by the caller.
    labels = snapshot["labels"]
    assert labels["omnigent.last_context_at"].isdigit(), (
        f"fill recorded without a measurement time: labels={sorted(labels)}"
    )
    assert isinstance(snapshot["updated_at"], int)

    info = _get_info_against(live_server, session_id)

    # Same numbers, reached through the tool an agent actually calls.
    assert info["context"]["tokens"] == tokens
    assert info["context"]["as_of"] == int(labels["omnigent.last_context_at"])
    assert info["context"]["age_seconds"] >= 0
    assert info["created_at"] == snapshot["created_at"]
    assert info["updated_at"] == snapshot["updated_at"]

    # The turn reached a terminal edge, so its end time is recorded and the
    # elapsed time is derivable — the pair a cache-warmth decision needs.
    assert info["last_turn_completed_at"] is not None, (
        f"completed turn left no end timestamp: labels={sorted(labels)}"
    )
    assert info["seconds_since_last_turn"] >= 0
