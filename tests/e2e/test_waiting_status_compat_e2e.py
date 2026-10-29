"""Keep session reads healthy across delegation and requester-protocol refusal.

Compatible pairs dispatch a gated child and hold the parent's waiting window.
A current runner against a server without requester routes must explicitly
refuse, without child work or a legacy fallback. The runner component test
exercises the pre-0.3 status downgrade even when delegation is unavailable.
No minimum-version skip: both outcomes must remain covered in compatibility CI.
"""

from __future__ import annotations

import ast
import json
import time
import uuid

import httpx
import pytest

from tests._helpers.compat import compat_runner_python
from tests._helpers.polling import poll_until
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
)

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


@pytest.mark.compat_smoke
def test_runner_does_not_500_old_server_emitting_waiting_status(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    def session_ids() -> set[str]:
        ids = set()
        params = {"limit": 1000, "kind": "any", "include_archived": "true", "visibility": "all"}
        while True:
            response = http_client.get("/v1/sessions", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            ids.update(session["id"] for session in page["data"])
            if not page["has_more"]:
                return ids
            params["after"] = page["last_id"]

    # Invalid JSON is rejected before requester checks or session creation.
    # A wrong-method GET is inconclusive when the server mounts the SPA at /.
    before = session_ids()
    capability = http_client.post(
        "/v1/requester/sessions", content="{", headers={"Content-Type": "application/json"}
    )
    assert session_ids() == before, "capability probe must not create a session"
    assert capability.status_code in {404, 405, 422}, capability.text
    if capability.status_code == 422:
        assert capability.json()["detail"][0]["type"] == "json_invalid", capability.text
    expect_refusal = capability.status_code in {404, 405} and compat_runner_python() is None

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

    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_sys_session_send_tool_call("researcher", "probe", "Investigate")]},
            {"text": "Dispatch attempt finished."},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Researcher result.", "block": True}],
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

    def tool_outputs() -> list[str]:
        return [
            item.get("output", item.get("content"))
            for request in get_mock_requests(mock_llm_server_url, key=parent_model)
            for item in request.get("input", request.get("messages", []))
            if isinstance(item, dict)
            and (
                (item.get("type") == "function_call_output" and item.get("call_id") == "call_1")
                or (item.get("role") == "tool" and item.get("tool_call_id") == "call_1")
            )
        ]

    try:
        poll_until(tool_outputs, timeout_s=60)
        output = ast.literal_eval(tool_outputs()[-1])
        if expect_refusal:
            assert "upgrade server and runner together" in output["result"], output
        else:
            handle = output
            assert handle["kind"] == "sub_agent", output
            poll_until(lambda: get_mock_requests(mock_llm_server_url, key=researcher_model))
            pending = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5)
            pending.raise_for_status()
            assert pending.json()["pending"], "child must remain gated throughout the poll window"

        # Keep the whole window after observing the tool result, including when
        # delegation is refused. Never turn an unexpected dispatch failure into a pass.
        deadline = time.monotonic() + 30.0
        polls = 0
        while time.monotonic() < deadline:
            response = http_client.get(f"/v1/sessions/{session_id}")
            assert response.status_code == 200, response.text
            assert response.json()["status"] in _SERIALIZABLE_STATUSES
            children = http_client.get(f"/v1/sessions/{session_id}/child_sessions")
            assert children.status_code == 200, children.text
            child_ids = {child["id"] for child in children.json()["data"]}
            if expect_refusal:
                assert not child_ids, children.text
            else:
                assert handle["conversation_id"] in child_ids, children.text
            polls += 1
            time.sleep(1.0)

        assert polls >= 5, f"expected sustained session queries, got {polls}"
        if expect_refusal:
            assert get_mock_requests(mock_llm_server_url, key=researcher_model) == []
        items = http_client.get(f"/v1/sessions/{session_id}/items")
        assert items.status_code == 200, items.text
        assert "Dispatch attempt finished." in items.text, "parent must finish its dispatch turn"
    finally:
        release_mock_gate(mock_llm_server_url)
