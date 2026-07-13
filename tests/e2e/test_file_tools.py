"""E2E tests: file tools and markdown attachment.

``test_markdown_file_attachment`` runs against the mock LLM server.
``test_list_files_finds_uploaded_file`` and
``test_download_file_retrieves_content`` require a real LLM (the
bundled archer agent declares ``tools.builtins`` which the omnigent
single-file YAML format does not support for inline agents).

Usage::

    pytest tests/e2e/test_file_tools.py -v
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant text blocks from a terminal response."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def test_markdown_file_attachment(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Uploading and attaching a .md file works end-to-end.

    Verifies the full pipeline: file upload → input_file content
    block → content resolution (MIME type from filename) → LLM
    receives and responds.
    """
    model = f"mock-md-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"md-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a document analyst.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Ship feature, write tests, update docs by Friday."}],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    md_content = (
        b"# Project Plan\n\n## Goals\n\n- Ship the feature by Friday\n- Write tests\n- Update docs"
    )
    upload_resp = http_client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("plan.md", md_content, "text/markdown")},
    )
    upload_resp.raise_for_status()
    file_id = upload_resp.json()["id"]

    rid = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=[
            {
                "type": "input_text",
                "text": "Summarize this document in one sentence.",
            },
            {"type": "input_file", "file_id": file_id, "filename": "plan.md"},
        ],
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid, timeout=60
    )

    assert body["status"] == "completed", (
        f"Status: {body['status']!r}. Error: {body.get('error')}. Output: {body.get('output', [])}"
    )
    text = _extract_all_text(body)
    assert text.strip(), f"Agent produced no text. Output: {body.get('output', [])}"


# ── Real-LLM tests (require archer_agent with tools.builtins) ──


def _has_tool_call(body: dict[str, Any], name: str) -> bool:
    """Check if a function_call with the given name exists in output."""
    return any(
        (i.get("type") == "function_call" and i.get("name") == name)
        or (i.get("event_type") == "tool_call" and i.get("tool_name") == name)
        for i in body.get("output", [])
    )


def _tool_outputs(body: dict[str, Any], name: str) -> list[str]:
    """Return outputs for completed tool calls named *name*."""
    call_ids = {
        item["call_id"]
        for item in body.get("output", [])
        if item.get("type") == "function_call" and item.get("name") == name
    }
    return [
        item["output"]
        for item in body.get("output", [])
        if item.get("type") == "function_call_output"
        and item.get("call_id") in call_ids
        and item.get("output", "").strip()
    ]


def test_list_files_finds_uploaded_file(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
) -> None:
    """A session-uploaded file is visible to list_files.

    Requires real LLM — archer agent with ``tools.builtins``.
    """
    if using_mock_llm:
        pytest.skip("requires real LLM (spec-level builtin tools)")

    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    upload_resp = http_client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("test_data.txt", b"Hello from omnigent", "text/plain")},
    )
    upload_resp.raise_for_status()

    rid = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use the list_files tool to show me all uploaded "
            "files. Only use list_files, nothing else."
        ),
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid, timeout=180
    )
    assert body["status"] == "completed", f"Turn failed: {body.get('error')}"

    assert _has_tool_call(body, "list_files"), "Agent didn't call list_files"
    assert any("test_data.txt" in output for output in _tool_outputs(body, "list_files")), (
        f"list_files didn't return uploaded file. Output: {body.get('output', [])}"
    )


def test_download_file_retrieves_content(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
) -> None:
    """download_file retrieves a session-uploaded file by ID.

    Requires real LLM — archer agent with ``tools.builtins``.
    """
    if using_mock_llm:
        pytest.skip("requires real LLM (spec-level builtin tools)")

    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    upload_resp = http_client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("greeting.txt", b"HELLO_WORLD", "text/plain")},
    )
    upload_resp.raise_for_status()
    file_id = upload_resp.json()["id"]

    rid = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Use download_file with file_id {file_id}. Do not call "
            "sys_os_shell, sys_os_read, or any other filesystem tool. "
            "Report the JSON result."
        ),
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid, timeout=180
    )
    assert body["status"] == "completed", f"Turn failed: {body.get('error')}"

    assert _has_tool_call(body, "download_file"), "Agent didn't call download_file"
    outputs = _tool_outputs(body, "download_file")
    assert outputs, f"download_file returned no tool output. Output: {body.get('output', [])}"
    assert any("HELLO_WORLD" in output for output in outputs), (
        f"download_file didn't return expected content. Tool outputs: {outputs}"
    )
