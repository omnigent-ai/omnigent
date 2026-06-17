"""
End-to-end journey test: file upload and agent analysis.

Verifies uploading markdown documents to a session and asking the
agent to analyze their contents. Runs against the mock LLM server.

Usage::

    pytest tests/e2e/test_journey_file_upload_analysis.py -v
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
)
from tests.e2e.helpers import final_assistant_text


def test_file_upload_and_analysis_journey(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Upload markdown files and verify the agent analyzes their content.

    Steps:

    1. Upload a markdown file with "The capital of Freedonia is Quuxville."
    2. Ask the agent; assert "Quuxville" in the response.
    3. Upload a second file with "The population of Freedonia is 42,000."
    4. Ask the agent about both; assert both facts appear.
    """

    model = f"mock-file-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"file-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a document analyst.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "According to the document, the capital of Freedonia is Quuxville."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # ── Step 1: Upload first markdown file ──────────────────────
    doc1_content = (
        b"# Freedonia Facts\n\nThe capital of Freedonia is Quuxville.\nIt was founded in 1847.\n"
    )
    upload1 = http_client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={
            "file": (
                "freedonia_capital.md",
                doc1_content,
                "text/markdown",
            )
        },
    )
    upload1.raise_for_status()
    file1_id = upload1.json()["id"]

    # ── Step 2: Ask agent about the first file ──────────────────
    response_id1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=[
            {
                "type": "input_text",
                "text": "What is the capital of Freedonia according to this document?",
            },
            {"type": "input_file", "file_id": file1_id},
        ],
    )
    body1 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id1,
        timeout=120,
    )
    assert body1["status"] == "completed", f"Turn 1 failed: {body1.get('error', 'unknown')}"
    text1 = final_assistant_text(body1).lower()
    assert "quuxville" in text1, f"Agent did not reference 'Quuxville'. Response:\n{text1}"

    # ── Step 3: Upload second markdown file ─────────────────────
    doc2_content = (
        b"# Freedonia Demographics\n\n"
        b"The population of Freedonia is 42,000.\n"
        b"The official language is Freedonian.\n"
    )

    # Reconfigure mock for the second turn.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": "Based on both documents: the capital of "
                "Freedonia is Quuxville and the population "
                "is 42,000.",
            },
        ],
        key=model,
    )

    session_id2 = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # Re-upload both files into the new session.
    reupload1 = http_client.post(
        f"/v1/sessions/{session_id2}/resources/files",
        files={
            "file": (
                "freedonia_capital.md",
                doc1_content,
                "text/markdown",
            )
        },
    )
    reupload1.raise_for_status()
    file1_id_s2 = reupload1.json()["id"]

    upload2 = http_client.post(
        f"/v1/sessions/{session_id2}/resources/files",
        files={
            "file": (
                "freedonia_demographics.md",
                doc2_content,
                "text/markdown",
            )
        },
    )
    upload2.raise_for_status()
    file2_id = upload2.json()["id"]

    # ── Step 4: Ask agent to use both files ─────────────────────
    response_id2 = send_user_message_to_session(
        http_client,
        session_id=session_id2,
        content=[
            {
                "type": "input_text",
                "text": "Based on the documents I uploaded, "
                "what is the capital and population of Freedonia?",
            },
            {"type": "input_file", "file_id": file1_id_s2},
            {"type": "input_file", "file_id": file2_id},
        ],
    )
    body2 = poll_session_until_terminal(
        http_client,
        session_id=session_id2,
        response_id=response_id2,
        timeout=120,
    )
    assert body2["status"] == "completed", f"Turn 2 failed: {body2.get('error', 'unknown')}"
    text2 = final_assistant_text(body2).lower()
    assert "quuxville" in text2, f"Agent did not mention 'Quuxville'. Response:\n{text2}"
    assert "42,000" in text2 or "42000" in text2, (
        f"Agent did not mention '42,000'. Response:\n{text2}"
    )
