"""E2E test: cancel → file attachment → cancel → file attachment → success.

Exercises the full cancel + file-attachment flow on the runner-native
sessions API (``POST /v1/sessions/{id}/events``). Verifies that:

1. Interrupting a running turn doesn't break subsequent turns.
2. File attachments work after an interrupt.
3. Multiple interrupt → send cycles don't corrupt session state.
4. The LLM actually reads the attached markdown content.

Migrated off the removed ``POST /v1/responses`` route: turns are now
driven through one runner-bound session, cancellation uses the
sessions interrupt event (the same path :mod:`test_cancel_history`
exercises), and continuity is implicit in the shared session rather
than threaded through ``previous_response_id``. File upload is
unchanged — ``POST /v1/sessions/{id}/resources/files``.

Usage::

    pytest tests/e2e/test_cancel_then_file_attachment.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
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
"""Distinctive fictional markdown — keyword assertions check for
'zebra', 'Mars' to confirm the file was actually read."""


def _upload_md(client: httpx.Client, session_id: str) -> str:
    """
    Upload the test markdown file and return its file_id.

    :param client: Sync HTTP client pointed at the live server.
    :param session_id: Owning session/conversation id.
    :returns: The uploaded file's ID.
    """
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


def _wait_for_session_running(client: httpx.Client, session_id: str, timeout: float = 60) -> None:
    """Poll until the runner-native session transitions to ``running``."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        last = resp.json()
        status = last.get("status")
        if status == "running":
            return
        if status not in ("idle", "running"):
            raise AssertionError(f"Session reached {status!r} before running: {last}")
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Session {session_id} didn't reach running within {timeout}s: {last}")


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


def test_cancel_send_file_cancel_send_file_succeeds(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
) -> None:
    """
    Sessions-API flow: send → interrupt → send with .md → interrupt →
    send with .md → verify content was read.

    All turns run in one runner-bound session, so conversation
    continuity is implicit. Cancellation uses the sessions interrupt
    event; the final turn must complete and quote distinctive terms
    from the uploaded markdown.

    **What breaks if wrong:**

    - Interrupt teardown leaves the session non-idle → the next
      ``events`` POST can't start a fresh turn.
    - Dangling ``function_call`` items without outputs after an
      interrupt → the next turn fails ``[llm] failed``.
    - The attached file never reaches the model → the final answer
      omits 'zebra'/'Mars'.

    :param http_client: Sync HTTP client for the live server.
    :param archer_agent: Name of the registered archer agent.
    :param live_runner_id: Registered runner id to bind the session to.
    :param using_mock_llm: True when the mock LLM backs the server.
    """
    if using_mock_llm:
        pytest.skip(
            "requires real streaming generation + file comprehension; "
            "the mock gate/interrupt interaction and the 'agent read the "
            "file' assertions do not reproduce under the mock LLM"
        )

    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    # ── Turn 1: start a long turn, interrupt mid-flight ───────────
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Write a detailed 2000-word essay about volcanoes.",
    )
    _wait_for_session_running(http_client, session_id)
    _interrupt_and_wait_idle(http_client, session_id)

    # ── Turn 2: send with markdown file, interrupt mid-flight ─────
    file_id_1 = _upload_md(http_client, session_id)
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=_file_message("Read this file and summarize it in detail.", file_id_1),
    )
    _wait_for_session_running(http_client, session_id)
    _interrupt_and_wait_idle(http_client, session_id)

    # ── Turn 3: send with markdown file again — must succeed ──────
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
        timeout=120,
    )

    assert body["status"] == "completed", (
        f"Turn 3 status: {body['status']!r}. Error: {body.get('error')}"
    )

    text = final_assistant_text(body)
    assert text.strip(), f"Agent produced no output. Body: {body}"

    # The content has distinctive terms that can only appear if the
    # LLM actually processed the uploaded markdown.
    text_lower = text.lower()
    assert "zebra" in text_lower, (
        f"Response should mention 'zebra' from the file. Got: {text[:300]}"
    )
    assert "mars" in text_lower, f"Response should mention 'Mars' from the file. Got: {text[:300]}"
