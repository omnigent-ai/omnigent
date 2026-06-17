"""End-to-end tests for the @tool decorator on real LLM + real server.

Verifies the full pipeline:
- Agent image with ``@tool``-decorated functions in
  ``tools/python/*.py`` is loaded into a real ``omnigent server``.
- Real LLM calls each tool with arguments inferred from the
  derived schema.
- Tool runs in a subprocess; result returns through the runner
  and gets persisted in the conversation.
- Final LLM response references the literal output values.

These tests require an LLM API key and a working ``ap`` CLI on
PATH; they are excluded from the default ``pytest`` run via
``--ignore=tests/e2e``.

**TUI verification** (mandatory per CLAUDE.md before merge):

- archer's word_count: ``python examples/frontends/terminal.py
  examples/archer/`` then ask "Count the words in this
  paragraph: <text>".
- decorator-signatures-test: ``python examples/frontends/terminal.py
  tests/_fixtures/agents/decorator-signatures-test/`` then ask
  "Greet Alice, format a record for Bob age 42, and compute
  with value 5".
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
)

_DECORATOR_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "_fixtures" / "agents" / "decorator-signatures-test"
)


@pytest.fixture(scope="session")
def decorator_signatures_agent(http_client: httpx.Client) -> str:
    """
    Upload the decorator-signatures-test fixture agent.

    :param http_client: HTTP client pointed at the live server.
    :returns: The agent's name (matches its config.yaml ``name``).
    """
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            tar.add(str(_DECORATOR_FIXTURE_DIR), arcname=".")
        bundle_path = tmp.name
    try:
        with open(bundle_path, "rb") as f:
            resp = http_client.post(
                "/v1/sessions",
                data={"metadata": json.dumps({})},
                files={
                    "bundle": (
                        "agent.tar.gz",
                        f,
                        "application/gzip",
                    ),
                },
            )
        if resp.status_code == 409:
            # Already registered from a prior test run in the same session.
            return _DECORATOR_FIXTURE_DIR.name
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        agent_resp = http_client.get(f"/v1/sessions/{session_id}/agent")
        agent_resp.raise_for_status()
        return agent_resp.json()["name"]
    finally:
        Path(bundle_path).unlink(missing_ok=True)


def _run_turn_in_session(
    http_client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
    user_text: str,
    timeout_s: float = 120.0,
) -> dict:
    """
    Create a runner-bound session, send one user turn, return the body.

    Wraps the runner-bound dispatch contract: create the
    session, PATCH a runner binding, POST the message through
    ``/events``, then poll the resolved response id to terminal state.

    :param http_client: HTTP client pointed at the live server.
    :param agent_name: Display name of an already-uploaded agent.
    :param runner_id: Registered runner id (the ``live_runner_id``
        fixture).
    :param user_text: Plain-text input message for the agent.
    :param timeout_s: Max seconds to wait for the response.
    :returns: The terminal response body.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=runner_id
    )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content=user_text
    )
    return poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=timeout_s,
    )


def _final_text(response_body: dict) -> str:
    """
    Extract the assistant's final text from a response.

    Walks ``output`` items, picks message items with role
    ``"assistant"``, and concatenates their ``output_text`` blocks.

    :param response_body: The response JSON returned from
        ``GET /v1/responses/{id}``.
    :returns: Concatenated assistant text. Empty string if no
        assistant message exists (which a passing test would
        catch via the content assertions).
    """
    parts: list[str] = []
    for item in response_body.get("output", []):
        if item.get("type") != "message":
            continue
        if item.get("role") != "assistant":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n\n".join(parts)


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_word_count_tool_e2e(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """
    archer + migrated ``word_count`` produces a correct count.

    Phrase chosen so the count is unambiguous: 7 words.
    """
    body = _run_turn_in_session(
        http_client,
        agent_name=archer_agent,
        runner_id=live_runner_id,
        user_text=(
            "Use the word_count tool to count the words in "
            "exactly this phrase: 'one two three four five six seven'. "
            "Tell me the number."
        ),
    )
    assert body["status"] == "completed", (
        f"archer turn did not complete: status={body.get('status')!r}, error={body.get('error')!r}"
    )
    final = _final_text(body)
    # The literal count must appear in the LLM's final response.
    # If "7" is missing, either word_count returned the wrong number
    # or the LLM didn't surface the result.
    assert "7" in final, f"Expected the count '7' in the final response, got: {final!r}"


def test_decorated_tools_varied_signatures_e2e(
    http_client: httpx.Client,
    decorator_signatures_agent: str,
    live_runner_id: str,
) -> None:
    """
    The decorator-signatures-test agent calls all three tools and
    surfaces literal output from each.

    Exercises:
    - Primitive arg (greet name='Alice').
    - Pydantic BaseModel arg (format_record name='Bob' age=42).
    - Multiple primitives + Annotated description (compute value=5).
    """
    body = _run_turn_in_session(
        http_client,
        agent_name=decorator_signatures_agent,
        runner_id=live_runner_id,
        user_text=(
            "Call all three tools: "
            "greet with name='Alice', "
            "format_record with name='Bob' age=42 (no email), "
            "and compute with value=5 (use the default multiplier). "
            "Then in your final response include the literal output "
            "values from each tool so I can verify them."
        ),
    )
    assert body["status"] == "completed", (
        f"signatures-test turn did not complete: "
        f"status={body.get('status')!r}, error={body.get('error')!r}"
    )
    final = _final_text(body)
    # Greet output: must contain "Alice" (literal name).
    assert "Alice" in final, (
        f"Final response missing 'Alice' from greet — either the tool "
        f"wasn't called or its result didn't surface. Got: {final!r}"
    )
    # format_record output: must contain "Bob" and "42".
    assert "Bob" in final, f"Missing 'Bob' from format_record. Got: {final!r}"
    assert "42" in final, f"Missing age '42' from format_record. Got: {final!r}"
    # compute output: must contain "10" (5 * 2 default multiplier).
    assert "10" in final, (
        f"Missing computed value '10' (5 * 2 default) — multiplier "
        f"default may not be honored, or compute wasn't called. "
        f"Got: {final!r}"
    )
