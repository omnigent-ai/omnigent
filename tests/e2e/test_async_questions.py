"""A question tool completes before an ordinary correlated user reply arrives."""

from __future__ import annotations

import json
import uuid

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)


def test_question_acknowledgement_and_later_answer_round_trip(
    http_client: httpx.Client, live_runner_id: str, mock_llm_server_url: str
) -> None:
    model = f"mock-questions-{uuid.uuid4().hex[:8]}"
    questions = [
        {"title": "Choose a route", "options": ["First", "Second"]},
        {"title": "Describe your answer"},
    ]
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "ask-question-call",
                        "name": "sys_ask_user",
                        "arguments": json.dumps({"questions": questions}),
                    }
                ]
            },
            {"text": "I can continue independent work while you answer."},
            {"text": "Answers received."},
        ],
        key=model,
    )
    agent = register_inline_agent(
        http_client,
        name=f"questions-{uuid.uuid4().hex[:8]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Ask questions with sys_ask_user and wait for answers before dependent work.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent, runner_id=live_runner_id
    )
    try:
        response_id = send_user_message_to_session(
            http_client, session_id=session_id, content="Ask me which route to take."
        )
        first = poll_session_until_terminal(
            http_client, session_id=session_id, response_id=response_id, timeout=60
        )
        assert first["status"] == "completed", first
        snapshot = http_client.get(f"/v1/sessions/{session_id}").json()
        prompts = [i for i in snapshot["items"] if i.get("data", {}).get("async_user_input")]
        assert len(prompts) == 1
        source = prompts[0]
        prompt = source["data"]["async_user_input"]
        assert len(prompt["reply_id"]) == 32
        assert not snapshot.get("pending_elicitations")
        reply = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Ignored client formatting"}],
                "async_question_reply": {
                    "source_item_id": source["id"],
                    "answers": {
                        f"{prompt['call_id']}:0": "Second",
                        f"{prompt['call_id']}:1": "My custom answer",
                    },
                    "cancelled": False,
                },
            },
        }
        posted = http_client.post(f"/v1/sessions/{session_id}/events", json=reply)
        posted.raise_for_status()
        answer_id = posted.json()["item_id"]
        items = http_client.get(f"/v1/sessions/{session_id}").json()["items"]
        answer = next(i for i in items if i["id"] == answer_id)
        assert answer["data"]["async_question_reply"] == reply["data"]["async_question_reply"]
        second = poll_session_until_terminal(
            http_client, session_id=session_id, response_id=answer["response_id"], timeout=60
        )
        assert second["status"] == "completed", second
        duplicate = http_client.post(f"/v1/sessions/{session_id}/events", json=reply)
        duplicate.raise_for_status()
        assert duplicate.json()["item_id"] == answer_id
        requests = json.dumps(get_mock_requests(mock_llm_server_url, key=model))
        assert "My custom answer" in requests
        assert "Ignored client formatting" not in requests
    finally:
        http_client.delete(f"/v1/sessions/{session_id}").raise_for_status()
