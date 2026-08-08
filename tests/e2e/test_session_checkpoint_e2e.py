"""Session checkpoint recovery across the live server and runner boundary."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Run a command.",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github__create_pull_request",
            "description": "Create a pull request.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _events(response: httpx.Response) -> Iterator[dict[str, Any]]:
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            line = next((part for part in frame.splitlines() if part.startswith("data:")), None)
            if line is None:
                continue
            try:
                yield json.loads(line.removeprefix("data:").strip())
            except json.JSONDecodeError:
                continue


def _run_turn(
    client: httpx.Client,
    *,
    base_url: str,
    session_id: str,
    content: str,
) -> list[str]:
    """Drive a real streamed turn and tunnel deterministic client-tool outputs."""
    calls: list[str] = []

    def post_user_message() -> None:
        with httpx.Client(base_url=base_url, timeout=30) as poster:
            send_user_message_to_session(poster, session_id=session_id, content=content, tools=_TOOLS)

    def post_tool_output(call_id: str, name: str, arguments: str) -> None:
        command = json.loads(arguments or "{}").get("command", "")
        if name == "Bash" and "git switch" in command:
            output = "Switched to a new branch 'feature/checkpoint'"
        elif name == "Bash" and "git commit" in command:
            output = "[feature/checkpoint abc1234] checkpoint"
        elif name == "Bash" and "git push" in command:
            output = (
                "To github.com:example/repository\n"
                " * [new branch]      feature/checkpoint -> feature/checkpoint"
            )
        elif name == "github__create_pull_request":
            output = '{"url":"https://github.com/example/repository/pull/42","number":42}'
        else:
            output = "Error: unexpected tool call"
        with httpx.Client(base_url=base_url, timeout=30) as poster:
            response = poster.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "function_call_output",
                    "data": {"call_id": call_id, "output": output},
                },
            )
            assert response.status_code in (200, 202), response.text[:300]

    with client.stream("GET", f"/v1/sessions/{session_id}/stream", timeout=120) as response:
        response.raise_for_status()
        posted = False
        completed = False
        for event in _events(response):
            if not posted:
                threading.Thread(target=post_user_message, daemon=True).start()
                posted = True
            if event.get("type") == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call" and item.get("status") == "action_required":
                    call_id = item.get("call_id")
                    if isinstance(call_id, str):
                        name = str(item.get("name"))
                        calls.append(name)
                        threading.Thread(
                            target=post_tool_output,
                            args=(call_id, name, str(item.get("arguments") or "")),
                            daemon=True,
                        ).start()
            elif event.get("type") == "response.completed":
                completed = True
                break
        assert completed, "runner turn did not complete"
    return calls


def test_checkpoint_resumes_pull_request_via_live_server_and_runner(
    live_server: str,
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Verified git work is persisted, pruned, and resumed as a PR-only turn."""
    suffix = uuid.uuid4().hex[:8]
    model = f"mock-checkpoint-{suffix}"
    agent_name = register_inline_agent(
        http_client,
        name=f"checkpoint-{suffix}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Use the supplied tools.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "branch",
                        "name": "Bash",
                        "arguments": json.dumps({"command": "git switch -c feature/checkpoint"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "commit",
                        "name": "Bash",
                        "arguments": json.dumps({"command": "git commit -m checkpoint"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "push",
                        "name": "Bash",
                        "arguments": json.dumps({"command": "git push origin feature/checkpoint"}),
                    }
                ]
            },
            {"text": "The branch, commit, and push completed."},
            {
                "tool_calls": [
                    {
                        "call_id": "pr",
                        "name": "github__create_pull_request",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": "Pull request created."},
        ],
        key=model,
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    first_calls = _run_turn(
        http_client,
        base_url=live_server,
        session_id=session_id,
        content=f"Prepare the branch, commit, and push checkpoint-{suffix}.",
    )
    checkpoint_response = http_client.get(f"/v1/sessions/{session_id}/checkpoint")
    deadline = time.monotonic() + 10
    while (
        checkpoint_response.status_code == 200
        and checkpoint_response.json().get("checkpoint") is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)
        checkpoint_response = http_client.get(f"/v1/sessions/{session_id}/checkpoint")
    assert checkpoint_response.status_code == 200
    checkpoint = checkpoint_response.json()["checkpoint"]
    assert checkpoint["phase"] == "open_pr"
    assert "Do not repeat the verified git commit." in checkpoint["do_not_repeat"]
    assert "Do not repeat the verified git push." in checkpoint["do_not_repeat"]
    assert first_calls == ["Bash", "Bash", "Bash"]

    second_calls = _run_turn(
        http_client,
        base_url=live_server,
        session_id=session_id,
        content="Just create a PR",
    )
    requests = get_mock_requests(mock_llm_server_url, key=model)
    second_request = next(
        request for request in reversed(requests) if "Just create a PR" in json.dumps(request)
    )
    model_input = json.dumps(second_request)

    assert second_calls == ["github__create_pull_request"]
    assert "Create the pull request with github__create_pull_request." in model_input
    assert "Do not repeat the verified git commit." in model_input
    assert "Do not repeat the verified git push." in model_input
    assert "git switch -c feature/checkpoint" not in model_input
    assert "git commit -m checkpoint" not in model_input
    assert "git push origin feature/checkpoint" not in model_input

    checkpoint_response = http_client.get(f"/v1/sessions/{session_id}/checkpoint")
    deadline = time.monotonic() + 10
    while (
        checkpoint_response.status_code == 200
        and (checkpoint_response.json().get("checkpoint") or {}).get("phase") != "complete"
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)
        checkpoint_response = http_client.get(f"/v1/sessions/{session_id}/checkpoint")
    final_checkpoint = checkpoint_response.json()["checkpoint"]
    assert final_checkpoint["phase"] == "complete"
    assert final_checkpoint["pr_url"] == "https://github.com/example/repository/pull/42"
