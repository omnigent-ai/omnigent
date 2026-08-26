"""E2E tests for restart-in-place — ``POST /v1/sessions/{id}/restart``.

Real server + runner + LLM (or mock LLM). Restart stops the session's current
harness execution and starts a fresh one on the SAME session id, from the
latest installed agent version, optionally rewinding the transcript to a
selected conversation point (``up_to_response_id``). The guarantees under
test:

* the session id, transcript, and metadata survive the restart, and the
  relaunched agent still recalls a code word planted before it;
* a truncation restart removes later items and keeps earlier ones.

The built-in version-refresh rebind (re-cloning a drifted fork/switch clone)
is covered by the route unit tests — an e2e server has no second built-in
bundle version to drift against.

In mock mode the source is an inline ``openai-agents`` agent pointed at the
mock LLM server; the recall queue is claimed by a token unique to the recall
turn (content-routing ``match``) so a stray request cannot drain it.

Usage::

    pytest tests/e2e/test_restart_session_e2e.py -v --timeout=60
"""

from __future__ import annotations

import uuid

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import final_assistant_text


def _bound_agent(client: httpx.Client, session_id: str) -> dict[str, str]:
    """Return the session's currently-bound agent object.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :returns: The agent object (``{id, name, harness, ...}``).
    """
    resp = client.get(f"/v1/sessions/{session_id}/agent")
    resp.raise_for_status()
    return resp.json()


def test_restart_session_in_place_recalls_history(
    http_client: httpx.Client,
    claude_coder_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
    mock_llm_server_url: str,
) -> None:
    """A restarted session recalls a code word planted before the restart.

    Plants a marker on a source session, restarts the session IN PLACE, then
    asks the relaunched agent to recall it. In mock mode the source agent is
    named like a fork of the ``sdk-chat-builtin`` built-in, so the restart
    also exercises the version refresh: the binding must move to a fresh
    clone of the current built-in bundle (agent id changes, root name stays).

    :param http_client: HTTP client pointed at the live server.
    :param claude_coder_agent: The uploaded claude-sdk source agent name
        (used only in real-LLM mode).
    :param live_runner_id: The server fixture's runner id.
    :param using_mock_llm: Whether mock LLM is active.
    :param mock_llm_server_url: Mock LLM server URL.
    :returns: None.
    """
    marker = f"RESTARTWORD_{uuid.uuid4().hex[:6].upper()}"

    if using_mock_llm:
        uid = uuid.uuid4().hex[:6]
        source_model = f"mock-restart-src-{uid}"
        source_agent = register_inline_agent(
            http_client,
            name=f"restart-src-{uid}",
            harness="openai-agents",
            model=source_model,
            profile="",
            prompt="You are a terse assistant.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        recall_token = f"recall-{uid}"
        recall_key = f"restart-tgt-{uid}"
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "ACK"}],
            key=source_model,
        )
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": marker}],
            key=recall_key,
            match=recall_token,
        )
        set_fallback_mock_llm(mock_llm_server_url, key=recall_key, text=marker)
    else:
        source_agent = claude_coder_agent

    # 1. Source session on the server's runner; plant a word.
    session_id = create_runner_bound_session(
        http_client, agent_name=source_agent, runner_id=live_runner_id
    )
    original_agent_id = _bound_agent(http_client, session_id)["id"]
    rid_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(f"Remember this code word for later: {marker}. Reply with exactly one word: ACK"),
    )
    body_1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid_1, timeout=180
    )
    assert body_1["status"] == "completed", f"plant turn failed: {body_1.get('error')}"
    pre_restart_item_ids = {
        item["id"] for item in http_client.get(f"/v1/sessions/{session_id}").json()["items"]
    }

    # 2. Restart the SAME session in place.
    resp = http_client.post(f"/v1/sessions/{session_id}/restart", json={})
    assert resp.status_code == 200, f"restart failed: {resp.status_code} {resp.text}"
    restarted = resp.json()
    # Same session — restart must not branch into a new conversation.
    assert restarted["id"] == session_id, "restart must keep the same session id"
    # Every pre-restart item survives. (Superset, not equality: the
    # relaunched runner appends its own init/resource items
    # asynchronously, so a later read can legitimately see MORE items.)
    restarted_item_ids = {item["id"] for item in restarted["items"]}
    assert pre_restart_item_ids <= restarted_item_ids, (
        "restart must not drop transcript items: "
        f"missing {sorted(pre_restart_item_ids - restarted_item_ids)}"
    )
    # A custom agent with no matching built-in keeps its binding (the
    # built-in refresh rebind is unit-tested at the route).
    assert restarted["agent_id"] == original_agent_id

    # 3. Recall on the SAME session after the relaunch. Only possible if the
    # in-place transcript was replayed as context to the restarted agent.
    recall_prompt = (
        "Earlier in this conversation I gave you a code word to remember. "
        "Reply with exactly that code word and nothing else."
    )
    if using_mock_llm:
        recall_prompt = f"{recall_prompt} ({recall_token})"
    rid_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=recall_prompt,
    )
    body_2 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid_2, timeout=180
    )
    assert body_2["status"] == "completed", f"recall turn failed: {body_2.get('error')}"
    text = final_assistant_text(body_2).upper()
    assert marker in text, (
        f"restarted agent did not recall {marker!r} (got {text!r}) — the transcript "
        "was not preserved or the relaunched agent was not rebuilt from it"
    )


def test_restart_session_truncates_to_response_point(
    http_client: httpx.Client,
    claude_coder_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
    mock_llm_server_url: str,
) -> None:
    """A restart with ``up_to_response_id`` rewinds the transcript.

    Plants two turns, then restarts the session up to the FIRST turn's
    response id. The restart response must list only the first turn's items —
    the later turn is gone from the Omnigent transcript the relaunched agent
    rebuilds from.

    :param http_client: HTTP client pointed at the live server.
    :param claude_coder_agent: The uploaded claude-sdk source agent name
        (used only in real-LLM mode).
    :param live_runner_id: The server fixture's runner id.
    :param using_mock_llm: Whether mock LLM is active.
    :param mock_llm_server_url: Mock LLM server URL.
    :returns: None.
    """
    if using_mock_llm:
        uid = uuid.uuid4().hex[:6]
        source_model = f"mock-restart-trunc-{uid}"
        source_agent = register_inline_agent(
            http_client,
            name=f"restart-trunc-{uid}",
            harness="openai-agents",
            model=source_model,
            profile="",
            prompt="You are a terse assistant.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "ACK"}, {"text": "ACK"}],
            key=source_model,
        )
    else:
        source_agent = claude_coder_agent

    session_id = create_runner_bound_session(
        http_client, agent_name=source_agent, runner_id=live_runner_id
    )
    rid_1 = send_user_message_to_session(
        http_client, session_id=session_id, content="First turn. Reply with exactly: ACK"
    )
    body_1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid_1, timeout=180
    )
    assert body_1["status"] == "completed", f"first turn failed: {body_1.get('error')}"
    items_after_turn_1 = http_client.get(f"/v1/sessions/{session_id}").json()["items"]
    # Turn-grouping response ids attach to the user message; assistant
    # output carries its own, so the restore point is the response id of
    # the LAST item of the first turn (what the UI passes when a user
    # picks that message).
    restore_point = next(
        item["response_id"] for item in reversed(items_after_turn_1) if item["response_id"]
    )

    rid_2 = send_user_message_to_session(
        http_client, session_id=session_id, content="Second turn. Reply with exactly: ACK"
    )
    body_2 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid_2, timeout=180
    )
    assert body_2["status"] == "completed", f"second turn failed: {body_2.get('error')}"
    items_after_turn_2 = http_client.get(f"/v1/sessions/{session_id}").json()["items"]
    turn_1_item_ids = {item["id"] for item in items_after_turn_1}
    turn_2_item_ids = {item["id"] for item in items_after_turn_2} - turn_1_item_ids
    assert turn_2_item_ids, "second turn must append items"

    # Rewind to the end of the first turn.
    resp = http_client.post(
        f"/v1/sessions/{session_id}/restart",
        json={"up_to_response_id": restore_point},
    )
    assert resp.status_code == 200, f"restart failed: {resp.status_code} {resp.text}"
    restarted = resp.json()
    assert restarted["id"] == session_id, "restart must keep the same session id"
    restarted_item_ids = {item["id"] for item in restarted["items"]}
    assert turn_1_item_ids <= restarted_item_ids, "truncation must keep the first turn"
    assert not (turn_2_item_ids & restarted_item_ids), "truncation must drop the second turn"
