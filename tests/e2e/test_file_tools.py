"""E2E test: list_files and download_file tools.

Verifies the full round-trip: agent creates a file with
sys_os_shell, uploads it with upload_file, then uses list_files
to find it and download_file to retrieve it.

Usage::

    pytest tests/e2e/test_file_tools.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
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


def _has_tool_call(body: dict[str, Any], name: str) -> bool:
    """
    Check if a function_call with the given name exists in output.

    :param body: The terminal response body.
    :param name: Tool name to find.
    :returns: True if found.
    """
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
) -> None:
    """
    A session-uploaded file is visible to list_files in the same session.

    :param http_client: HTTP client pointed at the live server.
    :param archer_agent: The registered archer agent name.
    """
    session_id = create_runner_bound_session(
        http_client,
        agent_name=archer_agent,
        runner_id=live_runner_id,
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
        http_client,
        session_id=session_id,
        response_id=rid,
        timeout=180,
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
) -> None:
    """
    download_file retrieves a session-uploaded file by ID.

    :param http_client: HTTP client pointed at the live server.
    :param archer_agent: The registered archer agent name.
    """
    session_id = create_runner_bound_session(
        http_client,
        agent_name=archer_agent,
        runner_id=live_runner_id,
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
            f"Use download_file with file_id {file_id}. Do not call sys_os_shell, "
            "sys_os_read, or any other filesystem tool. Report the JSON result."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=rid,
        timeout=180,
    )
    assert body["status"] == "completed", f"Turn failed: {body.get('error')}"

    assert _has_tool_call(body, "download_file"), "Agent didn't call download_file"
    outputs = _tool_outputs(body, "download_file")
    assert outputs, f"download_file returned no tool output. Output: {body.get('output', [])}"
    assert any("HELLO_WORLD" in output for output in outputs), (
        f"download_file didn't return expected content. Tool outputs: {outputs}"
    )


def test_markdown_file_attachment(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """
    Uploading and attaching a .md file works end-to-end.

    Verifies the full pipeline: file upload → input_file content
    block → content resolution (MIME type from filename) → LLM
    receives and understands the file content. Dispatched through
    a runner-bound session (the dispatch path archer ends up on
    after the model rewrite picks ``openai-agents`` as harness).

    **What breaks if this fails:**
    - File upload rejects .md files or stores wrong content_type.
    - Content resolver falls back to application/octet-stream
      (which OpenAI rejects for text files).
    - _normalize_input double-wraps message items.
    """
    session_id = create_runner_bound_session(
        http_client,
        agent_name=archer_agent,
        runner_id=live_runner_id,
    )

    # Upload a markdown file into the owning session.
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
            {"type": "input_text", "text": "Summarize this document in one sentence."},
            {"type": "input_file", "file_id": file_id, "filename": "plan.md"},
        ],
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=rid,
        timeout=60,
    )

    assert body["status"] == "completed", (
        f"Status: {body['status']!r}. Error: {body.get('error')}. Output: {body.get('output', [])}"
    )
    text = _extract_all_text(body)
    assert text.strip(), f"Agent produced no text. Output: {body.get('output', [])}"
