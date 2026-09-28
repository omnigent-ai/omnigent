"""Regression e2e — forking an SDK-harness session must not lose the tool
context the model needs (a fork that drops it "can't answer and keeps
pulling logs").

User journey (Web-UI fork flow):

1. Run a session whose agent calls a tool; the tool's OUTPUT is the only
   place a fact ever appears (never in a user/assistant message).
2. Fork that session (``POST /v1/sessions/{id}/fork`` + ``PATCH`` a runner
   onto the clone — exactly what the SPA "Clone session" button does).
3. Ask the fork about the fact the tool produced.

Observed failure: the forked agent never receives the recorded tool output,
so it cannot answer and re-invokes the tool ("keeps pulling logs").

Mechanism (background, NOT asserted here): every SDK harness routes history
through ``ExecutorAdapter._extract_role_keyed_messages``. On a *continued*
turn the inner SDK's own session still holds the tool items (Layer-1 state),
but a fork spins up a FRESH inner SDK session with no Layer-1 state — so the
replayed input itself must carry the recorded ``function_call`` /
``function_call_output`` history, or the model never sees the tool output
even though the fork's persisted conversation deep-copied it.

This test measures that signature directly: what the fork replays to the
model (the mock LLM captures every request body). If the fork stops carrying
tool history into the replayed input, the regression assertion at the bottom
fails again.

The ``decorator-tools`` fixture agent runs the ``openai-agents`` SDK harness
and ships a real ``greet`` tool as Python source, so the tool is dispatched by
the runner and recorded as a genuine ``function_call_output`` item — a
deterministic stand-in for the resolve agent's log-fetching tools.

Usage::

    NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \\
        pytest tests/e2e/test_fork_replays_tool_history.py -v
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    poll_session_until_terminal,
    register_dir_agent_with_mock_llm,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

# Fixture agent: openai-agents SDK harness, ships a real ``greet`` tool as
# Python source under tools/python/ (dispatched by the runner, recorded as a
# function_call_output). Its tool output is the deterministic stand-in for the
# resolve agent's fetched logs.
_DECORATOR_TOOLS_DIR = (
    Path(__file__).resolve().parents[1] / "resources" / "agents" / "decorator-tools"
)


def _fork_session(
    client: httpx.Client,
    source_id: str,
    *,
    runner_id: str,
) -> dict[str, Any]:
    """Fork *source_id* and bind the clone to *runner_id*.

    Mirrors the Web-UI "Clone session" flow: ``POST /fork`` creates an
    unbound clone, then ``PATCH`` binds a runner so turns can be dispatched.

    :param client: HTTP client pointed at the live server.
    :param source_id: Session to fork, e.g. ``"conv_abc"``.
    :param runner_id: Registered runner id to bind the fork to.
    :returns: The 201 fork response body (``SessionResponse`` shape).
    """
    resp = client.post(f"/v1/sessions/{source_id}/fork", json={})
    assert resp.status_code == 201, f"fork failed: {resp.status_code} {resp.text}"
    fork = resp.json()
    patch = client.patch(f"/v1/sessions/{fork['id']}", json={"runner_id": runner_id})
    patch.raise_for_status()
    return fork


def _session_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return the persisted conversation items for *session_id*."""
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json().get("items", [])


def _message_texts(items: list[dict[str, Any]]) -> str:
    """Concatenate the text of every ``message`` (user/assistant) item.

    Deliberately excludes ``function_call`` / ``function_call_output`` items so
    the setup guard can prove the secret lives ONLY in the tool output.
    """
    parts: list[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        for block in (item.get("data") or {}).get("content", []) or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


def _request_with_marker(requests: list[dict[str, Any]], marker: str) -> dict[str, Any]:
    """Return the captured model request whose body contains *marker*.

    Each turn's user question carries a unique marker, so the request the SDK
    made for that specific turn is identifiable among all captured requests.
    """
    for req in requests:
        if marker in json.dumps(req):
            return req
    raise AssertionError(f"no captured model request contained marker {marker!r}")


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_fork_carries_tool_output_context(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A fork must replay recorded tool output to the model, like a continued
    turn does.

    Drives the real Web-UI fork journey and measures what each turn replays to
    the model (the mock LLM captures every request body):

    - **Setup guard:** the secret appears ONLY in the ``greet`` tool's
      ``function_call`` / ``function_call_output`` items, never in a
      user/assistant message.
    - **Fork copy guard:** the fork deep-copied those tool items (the secret is
      in the clone's persisted conversation).
    - **Control:** a *continued* turn on the source replays the tool output to
      the model (its request contains the secret) — proving tool context
      normally reaches the model and the discriminator works.
    - **Regression assertion:** the fork's first turn must ALSO replay the
      tool output to the model. When it does not, the forked agent never sees
      the tool result, so it cannot answer and re-pulls the tool ("keeps
      pulling logs").
    """
    secret = f"ZORP-{uuid.uuid4().hex[:8].upper()}"
    model = f"mock-fork-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_dir_agent_with_mock_llm(
        http_client,
        agent_dir=_DECORATOR_TOOLS_DIR,
        name=f"forktool-{uuid.uuid4().hex[:6]}",
        model=model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # Turn 1: the model calls greet(name=<secret>); the real tool runs and its
    # output "Hello, <secret>!" is recorded as a function_call_output. The
    # secret is chosen by the tool call, never typed by the user, so it can
    # only ever reach a later turn's model via replayed tool history.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_greet_1",
                        "name": "greet",
                        "arguments": json.dumps({"name": secret}),
                    }
                ]
            },
            {"text": "I have greeted them."},
        ],
        key=model,
    )
    # Any later turn (the control + the fork recall) always gets a valid reply.
    set_fallback_mock_llm(mock_llm_server_url, model, "Let me check the logs again.")

    source_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    rid1 = send_user_message_to_session(
        http_client,
        session_id=source_id,
        content="Please greet the new hire using the greet tool.",
    )
    body1 = poll_session_until_terminal(
        http_client, session_id=source_id, response_id=rid1, timeout=120
    )
    assert body1["status"] == "completed", (
        f"seed turn did not complete: status={body1.get('status')!r} error={body1.get('error')!r}"
    )

    # Setup guard: the secret lives ONLY in the tool call/output, not in any
    # user/assistant message. If this fails the test proves nothing.
    src_items = _session_items(http_client, source_id)
    src_blob = json.dumps(src_items)
    assert secret in src_blob, (
        "precondition: the greet tool output was not recorded — "
        f"secret {secret!r} absent from source items"
    )
    assert secret not in _message_texts(src_items), (
        "precondition: the secret leaked into a user/assistant message; it must "
        "live ONLY in the tool output so the test measures tool-context replay"
    )
    tool_output_items = [
        it
        for it in src_items
        if it.get("type") == "function_call_output" and secret in json.dumps(it)
    ]
    assert tool_output_items, (
        "precondition: no function_call_output item carried the secret — "
        f"source item types: {[it.get('type') for it in src_items]!r}"
    )

    # Fork the session (Web-UI "Clone session" flow) BEFORE any recall turn.
    fork = _fork_session(http_client, source_id, runner_id=live_runner_id)
    assert fork["id"] != source_id

    # Fork copy guard: the tool output was deep-copied into the clone. So any
    # loss on the fork's turn is a REPLAY defect, not a copy defect.
    fork_items = _session_items(http_client, fork["id"])
    assert secret in json.dumps(fork_items), (
        "fork did not deep-copy the tool output into the clone's conversation; "
        f"fork item types: {[it.get('type') for it in fork_items]!r}"
    )

    parent_marker = f"PARENTQ-{uuid.uuid4().hex[:8]}"
    fork_marker = f"FORKQ-{uuid.uuid4().hex[:8]}"
    recall = "What exact name did you greet earlier? Reply with just the name."

    # Control: a CONTINUED turn on the source. The inner SDK session still
    # holds the tool items, so the model request replays the tool output.
    rid2 = send_user_message_to_session(
        http_client, session_id=source_id, content=f"{parent_marker} {recall}"
    )
    poll_session_until_terminal(http_client, session_id=source_id, response_id=rid2, timeout=120)

    # Regression: the FORK's first turn. If tool items are dropped from the
    # replay into the fresh inner SDK session, the tool output never reaches
    # the model.
    rid3 = send_user_message_to_session(
        http_client, session_id=fork["id"], content=f"{fork_marker} {recall}"
    )
    poll_session_until_terminal(http_client, session_id=fork["id"], response_id=rid3, timeout=120)

    requests = get_mock_requests(mock_llm_server_url, key=model)
    parent_turn_req = _request_with_marker(requests, parent_marker)
    fork_turn_req = _request_with_marker(requests, fork_marker)

    # Control assertion: a continued turn DOES replay the tool output. This
    # proves the discriminator works and that the loss below is fork-specific
    # (not an always-on tool-drop).
    assert secret in json.dumps(parent_turn_req), (
        "control failed: a continued (non-forked) turn did not replay the tool "
        "output to the model — the discriminator is invalid, cannot conclude"
    )

    # Regression assertion: the fork's first turn must replay the recorded
    # tool output to the model, exactly as a continued turn does.
    assert secret in json.dumps(fork_turn_req), (
        "fork tool-context regression: the forked session did NOT replay the recorded tool output "
        f"({secret!r}) to the model, even though the fork deep-copied it into "
        "the clone's conversation. The forked agent never receives the tool "
        "result, so it cannot answer and re-invokes the tool ('keeps pulling "
        "logs'). A continued turn on the same source DID replay it (control "
        "above passed), so the loss is fork-specific."
    )
