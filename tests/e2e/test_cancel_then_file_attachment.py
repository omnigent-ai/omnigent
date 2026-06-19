"""E2E test: cancel + file attachment flow (mock LLM).

Exercises the cancel + file-attachment flow on the runner-native
sessions API. Uses the mock LLM's block mode to hold turns in
running state so they can be interrupted, then verifies that a
subsequent turn with a file attachment completes normally.

The interrupt pattern follows test_cancel_history.py:
  1. Wait for the gate to be pending (LLM is blocked)
  2. Interrupt the session
  3. Release the gate (let the blocked request complete for cleanup)

The model name is static (mock-cancel-file) so reruns hit the same
mock queue key after reset_mock_llm.

Usage::

    pytest tests/e2e/test_cancel_then_file_attachment.py -v
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S, final_assistant_text

_MD_CONTENT = (
    b"# Zebra Deployment Protocol\n\n"
    b"## Overview\n\n"
    b"The Zebra Deployment Protocol (ZDP) is a fictional deployment\n"
    b"strategy used exclusively by the Interplanetary Logistics Corps\n"
    b"to deliver supply crates to Mars colonies.\n\n"
    b"## Key Steps\n\n"
    b"1. Load crates onto the orbital catapult.\n"
    b"2. Calibrate the zebra-stripe targeting laser.\n"
    b"3. Launch during the Tuesday alignment window.\n"
    b"4. Confirm delivery via carrier pigeon relay.\n"
)
"""Distinctive fictional markdown -- keyword assertions check for
'zebra', 'Mars' to confirm the pipeline delivered the file."""


def _upload_md(client: httpx.Client, session_id: str) -> str:
    """Upload the test markdown file and return its file_id."""
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("protocol.md", _MD_CONTENT, "text/markdown")},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _file_message(text: str, file_id: str) -> list[dict[str, Any]]:
    """User-message content blocks pairing a prompt with an attachment."""
    return [
        {"type": "input_text", "text": text},
        {"type": "input_file", "file_id": file_id, "filename": "protocol.md"},
    ]


def _wait_for_gate_pending(mock_url: str, timeout: float = 30) -> None:
    """Poll until a request is blocked on the mock LLM gate."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_url}/gate/pending", timeout=2.0)
        resp.raise_for_status()
        if resp.json().get("pending"):
            return
        time.sleep(0.1)
    raise AssertionError(f"No gate pending within {timeout}s")


def _interrupt_and_wait_idle(client: httpx.Client, session_id: str, timeout: float = 30) -> None:
    """Interrupt the running turn and wait for the session to settle idle."""
    cancel = client.post(f"/v1/sessions/{session_id}/events", json={"type": "interrupt"})
    cancel.raise_for_status()
    assert cancel.status_code in (202, 204), f"Unexpected interrupt status: {cancel.status_code}"
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        last = resp.json()
        status = last.get("status")
        if status == "failed":
            raise AssertionError(f"Session failed during interrupt teardown: {last}")
        if status == "idle":
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Session {session_id} did not return to idle within {timeout}s: {last}")


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_cancel_send_file_cancel_send_file_succeeds(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Sessions-API flow: send -> interrupt -> send .md -> interrupt -> send .md -> verify.

    :param http_client: Sync HTTP client for the live server.
    :param live_runner_id: Registered runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"cancel-file-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model="mock-cancel-file",
        profile="",
        prompt="You are a document analyst. Read files and answer questions.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {"block": True, "text": "A long essay about volcanoes..."},
            {"block": True, "text": "Summarizing the file..."},
            {
                "text": (
                    "The document describes the Zebra Deployment Protocol (ZDP), "
                    "a fictional strategy used by the Interplanetary Logistics "
                    "Corps to deliver supply crates to Mars colonies. The protocol "
                    "involves an orbital catapult and a zebra-stripe targeting laser."
                ),
            },
        ],
        key="mock-cancel-file",
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    # Turn 1: start, wait for gate (LLM blocked), interrupt, then release gate.
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Write a detailed 2000-word essay about volcanoes.",
    )
    _wait_for_gate_pending(mock_llm_server_url)
    _interrupt_and_wait_idle(http_client, session_id)
    release_mock_gate(mock_llm_server_url)

    # Turn 2: send with markdown file, interrupt while blocked.
    file_id_1 = _upload_md(http_client, session_id)
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=_file_message("Read this file and summarize it in detail.", file_id_1),
    )
    _wait_for_gate_pending(mock_llm_server_url)
    _interrupt_and_wait_idle(http_client, session_id)
    release_mock_gate(mock_llm_server_url)

    # Turn 3: send with markdown file -- must succeed.
    file_id_2 = _upload_md(http_client, session_id)
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=_file_message(
            "Read this file and tell me: what is the name of the protocol, "
            "what planet does it target, and what animal is in the name? "
            "Answer in one sentence.",
            file_id_2,
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )

    assert body["status"] == "completed", (
        f"Turn 3 status: {body['status']!r}. Error: {body.get('error')}"
    )

    text = final_assistant_text(body)
    assert text.strip(), f"Agent produced no output. Body: {body}"

    text_lower = text.lower()
    assert "zebra" in text_lower, (
        f"Response should mention 'zebra' from the file. Got: {text[:300]}"
    )
    assert "mars" in text_lower, f"Response should mention 'Mars' from the file. Got: {text[:300]}"
