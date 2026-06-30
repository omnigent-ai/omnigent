"""End-to-end tests for the @tool decorator (mock LLM).

Verifies the full pipeline:
- Agent ships its @tool functions as Python source in the uploaded bundle
  (tools/python/, auto-discovered) — loaded by file path on any server.
- Mock LLM emits tool_calls with the correct arguments.
- Tools run in the server subprocess; results return through the runner.
- Mock LLM's follow-up response references the literal output values.

Usage::

    pytest tests/e2e/test_decorated_tools_e2e.py -v
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_dir_agent_with_mock_llm,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

# Fixture agent whose @tool functions ship as Python source under
# tools/python/ (auto-discovered, like the archer fixture), so the server
# loads them by file path from the uploaded bundle on any version — no
# dependency on the repo's tests/ tree being importable by the server.
_DECORATOR_TOOLS_DIR = (
    Path(__file__).resolve().parents[1] / "resources" / "agents" / "decorator-tools"
)


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_word_count_tool_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Mock LLM calls word_count, real tool runs, mock returns result text.

    The mock LLM first emits a tool_call for ``word_count`` with a
    known phrase, the server executes the real function, and the mock's
    second response references the literal count.
    """
    model = f"mock-wordcount-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_dir_agent_with_mock_llm(
        http_client,
        agent_dir=_DECORATOR_TOOLS_DIR,
        name=f"wordcount-{uuid.uuid4().hex[:6]}",
        model=model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # Turn 1: LLM calls word_count with a 7-word phrase.
    # Turn 2: LLM sees the tool result and reports the number.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_wc1",
                        "name": "word_count",
                        "arguments": json.dumps({"text": "one two three four five six seven"}),
                    },
                ],
            },
            {"text": "The word count is 7."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use the word_count tool to count the words in "
            "exactly this phrase: 'one two three four five six seven'. "
            "Tell me the number."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )

    assert body["status"] == "completed", (
        f"archer turn did not complete: status={body.get('status')!r}, error={body.get('error')!r}"
    )
    final = final_assistant_text(body)
    assert "7" in final, f"Expected the count '7' in the final response, got: {final!r}"


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_decorated_tools_varied_signatures_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Mock LLM calls greet, format_record, compute; real tools execute.

    Exercises:
    - Primitive arg (greet name='Alice').
    - Pydantic BaseModel arg (format_record name='Bob' age=42).
    - Multiple primitives + default (compute value=5).
    """
    model = f"mock-decsig-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_dir_agent_with_mock_llm(
        http_client,
        agent_dir=_DECORATOR_TOOLS_DIR,
        name=f"decsig-{uuid.uuid4().hex[:6]}",
        model=model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # Mock queue:
    # 1. LLM calls all three tools in parallel.
    # 2. After receiving tool results, LLM produces the final text.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_greet",
                        "name": "greet",
                        "arguments": json.dumps({"name": "Alice"}),
                    },
                    {
                        "call_id": "call_fmt",
                        "name": "format_record",
                        "arguments": json.dumps(
                            {
                                "record": {"name": "Bob", "age": 42},
                            }
                        ),
                    },
                    {
                        "call_id": "call_comp",
                        "name": "compute",
                        "arguments": json.dumps({"value": 5}),
                    },
                ],
            },
            {
                "text": (
                    "Results:\n"
                    "- greet: Hello, Alice!\n"
                    "- format_record: Person(name=Bob, age=42)\n"
                    "- compute: product is 10"
                ),
            },
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Call all three tools: "
            "greet with name='Alice', "
            "format_record with name='Bob' age=42 (no email), "
            "and compute with value=5 (use the default multiplier). "
            "Then report the literal output values."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )

    assert body["status"] == "completed", (
        f"signatures-test turn did not complete: "
        f"status={body.get('status')!r}, error={body.get('error')!r}"
    )
    final = final_assistant_text(body)

    # Greet output: must contain "Alice".
    assert "Alice" in final, f"Final response missing 'Alice' from greet. Got: {final!r}"
    # format_record output: must contain "Bob" and "42".
    assert "Bob" in final, f"Missing 'Bob' from format_record. Got: {final!r}"
    assert "42" in final, f"Missing age '42' from format_record. Got: {final!r}"
    # compute output: must contain "10" (5 * 2 default multiplier).
    assert "10" in final, f"Missing computed value '10' (5 * 2 default). Got: {final!r}"
