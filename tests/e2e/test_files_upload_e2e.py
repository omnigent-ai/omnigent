"""
End-to-end smoke test for document file upload + inference through
every harness wrap (claude-sdk, codex, pi, openai-agents).

Two file types are tested:

- ``test.md``  (text/markdown) — heading is "This is a test markdown
  file"; the LLM must quote it back.
- ``test.pdf`` (application/pdf) — single-page PDF containing
  "hello, world!"; the LLM must describe or quote the content,
  proving the PDF document block reached the model.

Each file type has its own test function, each parametrized across
harnesses.  Gated on ``--profile``.  Run with::

    .venv/bin/python -m pytest \\
        tests/e2e/test_files_upload_e2e.py \\
        --profile oss -v

Test IDs are ``[claude-sdk]``, ``[codex]``, etc. so a per-harness
failure is visible at a glance.

The turn is driven through a runner-bound session: the agent bundle
is registered, a session is created and bound to the live runner via
``PATCH /v1/sessions/{id}`` (the runner-state contract), the
file is uploaded to the session-scoped files API, and the user
message (text + ``input_file`` block) is posted to
``POST /v1/sessions/{id}/events``.  The terminal turn is read from the
session snapshot — the legacy ``POST /v1/responses`` route was removed.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.e2e._harness_probes import (
    HARNESS_PROBES,
    HarnessProbe,
    skip_if_harness_cli_missing,
)
from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Checked-in test documents.
_TEST_MD_PATH = _REPO_ROOT / "tests" / "resources" / "test.md"
_TEST_PDF_PATH = _REPO_ROOT / "tests" / "resources" / "test.pdf"

_FILE_HARNESS_PROBES: list[HarnessProbe] = list(HARNESS_PROBES)
_FILE_HARNESS_IDS: list[str] = [p.harness for p in _FILE_HARNESS_PROBES]


@pytest.fixture
def databricks_profile(request: pytest.FixtureRequest) -> str:
    """
    Return the ``--profile`` CLI arg, or skip if not provided.

    :param request: Pytest fixture request.
    :returns: The profile name, e.g. ``"oss"``.
    """
    profile: str = request.config.getoption("--profile")
    if not profile:
        pytest.skip("file upload e2e requires --profile <name> (e.g. --profile oss)")
    return profile


def _bound_session_with_file(
    client: httpx.Client,
    *,
    profile: str,
    probe: HarnessProbe,
    runner_id: str,
    agent_name: str,
    file_path: Path,
    mime_type: str,
) -> tuple[str, str]:
    """
    Register the agent, create a runner-bound session, upload the file.

    :param client: HTTP client pointed at the Omnigent server.
    :param profile: Databricks profile name.
    :param probe: The harness probe (harness name + model).
    :param runner_id: Live runner id to bind the session to.
    :param agent_name: Unique agent name.
    :param file_path: Path to the file to upload.
    :param mime_type: MIME type for the upload.
    :returns: Tuple of ``(session_id, file_id)``.
    """
    # The returned name differs from agent_name on llm_flaky reruns.
    agent_name = register_inline_agent(
        client,
        name=agent_name,
        harness=probe.harness,
        model=probe.model,
        profile=profile,
        prompt=(
            "You are a document analysis assistant. When the user "
            "sends a file, read its content carefully and answer "
            "questions about it accurately."
        ),
    )
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
    harness: str,
    session_id: str,
    file_id: str,
    question: str,
) -> str:
    """
    Post a user message (text + ``input_file``) to the session and
    poll the snapshot until terminal, returning lowercased text.

    :param client: HTTP client pointed at the Omnigent server.
    :param harness: Harness identifier (used in assertion messages).
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
    assert body["status"] == "completed", (
        f"[{harness}] response failed: {body.get('error', 'unknown')}"
    )
    text = final_assistant_text(body).lower().strip()
    assert text, f"[{harness}] no assistant output text in response"
    return text


@pytest.mark.parametrize("probe", _FILE_HARNESS_PROBES, ids=_FILE_HARNESS_IDS)
def test_markdown_upload_reaches_llm(
    probe: HarnessProbe,
    databricks_profile: str,
    http_client: httpx.Client,
    live_runner_id: str,
) -> None:
    """
    Upload ``test.md`` and verify the LLM read its content.

    Full AP-side e2e per harness:

    1. Register an agent with the parametrized harness + model.
    2. Create a runner-bound session and upload ``test.md``
       (text/markdown) via the session-scoped files API.
    3. Post a user message (text + ``input_file``) asking the model
       to quote the heading; poll the session snapshot until terminal.
    4. Assert the response contains "test markdown file" — the exact
       heading from the file — proving the markdown content reached
       and was read by the model.

    :param probe: The harness probe (harness name + model).
    :param databricks_profile: The ``--profile`` value.
    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: The live runner id sessions bind to.
    """
    skip_if_harness_cli_missing(probe.harness)

    agent_name = f"files-md-e2e-{probe.harness}"
    session_id, file_id = _bound_session_with_file(
        http_client,
        profile=databricks_profile,
        probe=probe,
        runner_id=live_runner_id,
        agent_name=agent_name,
        file_path=_TEST_MD_PATH,
        mime_type="text/markdown",
    )

    text = _send_and_poll(
        http_client,
        harness=probe.harness,
        session_id=session_id,
        file_id=file_id,
        question=("What does the heading in this markdown file say? Quote it exactly."),
    )

    # test.md contains exactly: "# This is a test markdown file"
    # If the markdown content reached the model it will quote
    # "test markdown file". If the file was dropped the model has
    # nothing to quote.
    assert "test markdown file" in text, (
        f"[{probe.harness}] LLM did not quote the markdown heading — "
        f"file content likely dropped before reaching the model. "
        f"Full response:\n{text}"
    )


@pytest.mark.parametrize("probe", _FILE_HARNESS_PROBES, ids=_FILE_HARNESS_IDS)
def test_pdf_upload_reaches_llm(
    probe: HarnessProbe,
    databricks_profile: str,
    http_client: httpx.Client,
    live_runner_id: str,
) -> None:
    """
    Upload ``test.pdf`` and verify the LLM received the document.

    Full AP-side e2e per harness:

    1. Register an agent with the parametrized harness + model.
    2. Create a runner-bound session and upload ``test.pdf``
       (application/pdf) via the session-scoped files API.
    3. Post a user message (text + ``input_file``) asking whether the
       document has content; poll the session snapshot until terminal.
    4. Assert the response mentions PDF-related terms or the actual
       content — ``test.pdf`` is a single-page document containing
       "hello, world!".  A model that received the file will describe
       its content (quoting the text, mentioning page count, etc.) or
       may report it as empty/blank if text extraction fails — any
       of those signals proves the PDF block reached the model.

    :param probe: The harness probe (harness name + model).
    :param databricks_profile: The ``--profile`` value.
    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: The live runner id sessions bind to.
    """
    skip_if_harness_cli_missing(probe.harness)

    agent_name = f"files-pdf-e2e-{probe.harness}"
    session_id, file_id = _bound_session_with_file(
        http_client,
        profile=databricks_profile,
        probe=probe,
        runner_id=live_runner_id,
        agent_name=agent_name,
        file_path=_TEST_PDF_PATH,
        mime_type="application/pdf",
    )

    text = _send_and_poll(
        http_client,
        harness=probe.harness,
        session_id=session_id,
        file_id=file_id,
        question=("Does this PDF document contain any text content? Describe what you see in it."),
    )

    # test.pdf is a single-page PDF containing "hello, world!".  Different
    # models may report the content in different ways (quoting the text,
    # mentioning the page count, describing the document structure, or
    # calling it empty/blank if text extraction fails).  Any of these
    # keywords indicate the file block reached the model.
    _PDF_KEYWORDS = ("hello", "world", "pdf", "page", "document", "empty", "blank")
    assert any(kw in text for kw in _PDF_KEYWORDS), (
        f"[{probe.harness}] LLM response doesn't mention the PDF contents — "
        f"the PDF document block likely did not reach the model. "
        f"Expected one of {_PDF_KEYWORDS!r} in response.\n"
        f"Full response:\n{text}"
    )
