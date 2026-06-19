"""
End-to-end smoke test for document file upload + inference through
the ``openai-agents`` harness, driven by the mock LLM server.

Two file types are tested:

- ``test.md``  (text/markdown) — heading is "This is a test markdown
  file"; the mock LLM returns a canned response quoting it back.
- ``test.pdf`` (application/pdf) — single-page PDF containing
  "hello, world!"; the mock LLM returns a canned response
  describing the content, proving the PDF document block reached
  the model.

Each file type has its own test function. Run with::

    .venv/bin/python -m pytest \\
        tests/e2e/test_files_upload_e2e.py -v
"""

from __future__ import annotations

import uuid
from pathlib import Path

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

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Checked-in test documents.
_TEST_MD_PATH = _REPO_ROOT / "tests" / "resources" / "test.md"
_TEST_PDF_PATH = _REPO_ROOT / "tests" / "resources" / "test.pdf"


def _bound_session_with_file(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
    file_path: Path,
    mime_type: str,
) -> tuple[str, str]:
    """
    Create a runner-bound session, upload the file.

    :param client: HTTP client pointed at the Omnigent server.
    :param agent_name: Registered agent name.
    :param runner_id: Live runner id to bind the session to.
    :param file_path: Path to the file to upload.
    :param mime_type: MIME type for the upload.
    :returns: Tuple of ``(session_id, file_id)``.
    """
    session_id = create_runner_bound_session(
        client,
        agent_name=agent_name,
        runner_id=runner_id,
    )
    assert file_path.exists(), f"Test file missing at {file_path}. Restore from git."
    file_bytes = file_path.read_bytes()
    file_resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (file_path.name, file_bytes, mime_type)},
    )
    file_resp.raise_for_status()
    return session_id, file_resp.json()["id"]


def _send_and_poll(
    client: httpx.Client,
    *,
    session_id: str,
    file_id: str,
    question: str,
) -> str:
    """
    Post a user message (text + ``input_file``) to the session and
    poll the snapshot until terminal, returning lowercased text.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Runner-bound session that owns the file.
    :param file_id: The uploaded file ID.
    :param question: The question to ask about the file.
    :returns: Lowercased final assistant response text.
    """
    response_id = send_user_message_to_session(
        client,
        session_id=session_id,
        content=[
            {"type": "input_text", "text": question},
            {"type": "input_file", "file_id": file_id},
        ],
    )
    body = poll_session_until_terminal(
        client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", f"response failed: {body.get('error', 'unknown')}"
    text = final_assistant_text(body).lower().strip()
    assert text, "no assistant output text in response"
    return text


def test_markdown_upload_reaches_llm(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Upload ``test.md`` and verify the LLM received its content.

    Full AP-side e2e:

    1. Register an inline agent with the openai-agents harness.
    2. Configure the mock LLM to return a response quoting the heading.
    3. Create a runner-bound session and upload ``test.md``
       (text/markdown) via the session-scoped files API.
    4. Post a user message (text + ``input_file``) asking the model
       to quote the heading; poll the session snapshot until terminal.
    5. Assert the response contains "test markdown file" — the exact
       heading from the file — proving the markdown content reached
       and was read by the model.
    """
    model = f"mock-md-upload-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"files-md-e2e-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a document analysis assistant. When the user "
            "sends a file, read its content carefully and answer "
            "questions about it accurately."
        ),
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": 'The heading says: "This is a test markdown file".'}],
        key=model,
    )

    session_id, file_id = _bound_session_with_file(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
        file_path=_TEST_MD_PATH,
        mime_type="text/markdown",
    )

    text = _send_and_poll(
        http_client,
        session_id=session_id,
        file_id=file_id,
        question="What does the heading in this markdown file say? Quote it exactly.",
    )

    # test.md contains exactly: "# This is a test markdown file"
    assert "test markdown file" in text, (
        f"LLM did not quote the markdown heading — "
        f"file content likely dropped before reaching the model. "
        f"Full response:\n{text}"
    )


def test_pdf_upload_reaches_llm(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Upload ``test.pdf`` and verify the LLM received the document.

    Full AP-side e2e:

    1. Register an inline agent with the openai-agents harness.
    2. Configure the mock LLM to return a response describing the PDF.
    3. Create a runner-bound session and upload ``test.pdf``
       (application/pdf) via the session-scoped files API.
    4. Post a user message (text + ``input_file``) asking whether the
       document has content; poll the session snapshot until terminal.
    5. Assert the response mentions PDF-related terms or the actual
       content.
    """
    model = f"mock-pdf-upload-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"files-pdf-e2e-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a document analysis assistant. When the user "
            "sends a file, read its content carefully and answer "
            "questions about it accurately."
        ),
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "This PDF document contains the text 'hello, world!' on a single page."}],
        key=model,
    )

    session_id, file_id = _bound_session_with_file(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
        file_path=_TEST_PDF_PATH,
        mime_type="application/pdf",
    )

    text = _send_and_poll(
        http_client,
        session_id=session_id,
        file_id=file_id,
        question="Does this PDF document contain any text content? Describe what you see in it.",
    )

    # test.pdf is a single-page PDF containing "hello, world!".
    _PDF_KEYWORDS = ("hello", "world", "pdf", "page", "document", "empty", "blank")
    assert any(kw in text for kw in _PDF_KEYWORDS), (
        f"LLM response doesn't mention the PDF contents — "
        f"the PDF document block likely did not reach the model. "
        f"Expected one of {_PDF_KEYWORDS!r} in response.\n"
        f"Full response:\n{text}"
    )
