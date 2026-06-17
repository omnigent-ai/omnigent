"""E2E test: openai-coder agent uses client-side tools to list and manipulate files.

The openai-coder agent should be able to use client-side tools
(Read, Write, Edit, Glob, Grep, Bash) when the frontend passes
them. This test verifies that the agent invokes those tools through
the runner-native session-events path.

These tests drive client-side tools the way the real REPL does: they
subscribe to the session's live SSE stream and respond to
``action_required`` function_calls with ``function_call_output``
events. Snapshot polling cannot be used — ``action_required`` calls are
published to the live stream but never persisted to conversation items.

Usage::

    pytest tests/e2e/test_openai_coder_client_tools.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

# Load the coder tool set for client-side tool execution.
from omnigent.client_tools import get_tool_set as _get_tool_set
from tests.e2e.conftest import (
    create_runner_bound_session,
    send_user_message_to_session,
)

_tool_mod = _get_tool_set("coding")
TOOLS: list[dict[str, Any]] = _tool_mod.TOOLS
execute_tool = _tool_mod.execute_tool

# Names of the client-side tools this agent can tunnel. Only
# ``action_required`` calls for these are executed locally; everything
# else (server-side builtins, codex MCP) the runner dispatches itself.
_CLIENT_TOOL_NAMES: set[str] = {
    schema["function"]["name"] for schema in TOOLS if "function" in schema
}


def _iter_sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    """
    Yield decoded SSE event dicts from a streaming session response.

    :param response: An open streaming response from
        ``GET /v1/sessions/{id}/stream``.
    :returns: An iterator of parsed event dicts; stops at ``[DONE]``.
    """
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")),
                None,
            )
            if data_line is None:
                continue
            payload = data_line[len("data:") :].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def _run_with_tunneling(
    http_client: httpx.Client,
    *,
    base_url: str,
    agent_name: str,
    runner_id: str,
    user_input: str,
    timeout_s: float = 240.0,
) -> dict[str, Any]:
    """
    Run a client-tool turn by consuming the live session SSE stream.

    Mirrors how the real REPL drives client-side tools (and the SDK's
    ``SessionsChat``): subscribe to the session's live event stream,
    post the user message with the coder ``TOOLS``, and when the agent
    emits an ``action_required`` ``function_call`` for a client-side
    tool, execute it locally and post a ``function_call_output`` event
    so the parked turn resumes.

    Snapshot polling is deliberately NOT used here: ``action_required``
    function_calls are only published to the live stream, never
    persisted to conversation items (``_extract_persistent_item_from_sse``
    skips non-completed function_calls). The terminal turn body is
    assembled from the stream events — function_calls observed plus the
    outputs this client produced — so the assertions check what actually
    tunneled rather than relying on snapshot persistence.

    :param http_client: HTTP client pointed at the live server; holds
        the streaming GET open for the turn's duration.
    :param base_url: Server base URL for the short-lived POST clients
        (message + tool results), since ``http_client`` is monopolized
        by the streaming GET.
    :param agent_name: Display name of the uploaded agent.
    :param runner_id: Registered runner id.
    :param user_input: The user message.
    :param timeout_s: Max seconds to hold the stream open.
    :returns: A Responses-style terminal body: ``status`` plus an
        ``output`` list of function_call / function_call_output /
        assistant-message items assembled from the stream.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=runner_id
    )

    function_calls: dict[str, dict[str, Any]] = {}
    tool_outputs: dict[str, str] = {}
    text_chunks: list[str] = []
    status = "failed"

    def _post_user_message() -> None:
        with httpx.Client(base_url=base_url, timeout=30) as poster:
            send_user_message_to_session(
                poster, session_id=session_id, content=user_input, tools=TOOLS
            )

    def _post_tool_output(call_id: str, output: str) -> None:
        with httpx.Client(base_url=base_url, timeout=30) as poster:
            resp = poster.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "function_call_output",
                    "data": {"call_id": call_id, "output": output},
                },
            )
            assert resp.status_code in (200, 202), (
                f"function_call_output POST failed: {resp.status_code} {resp.text[:300]}"
            )

    with http_client.stream(
        "GET", f"/v1/sessions/{session_id}/stream", timeout=timeout_s
    ) as response:
        response.raise_for_status()
        posted = False
        for event in _iter_sse(response):
            # Post the user message only after the first frame confirms
            # the live-tail subscription is registered — the stream does
            # not replay history, so posting earlier could drop events.
            if not posted:
                threading.Thread(target=_post_user_message, daemon=True).start()
                posted = True

            etype = event.get("type")
            if etype == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    name = item.get("name")
                    function_calls[call_id] = {
                        "type": "function_call",
                        "name": name,
                        "call_id": call_id,
                    }
                    # Client-side tool parked: execute locally and tunnel
                    # the result back so the turn resumes.
                    if (
                        item.get("status") == "action_required"
                        and name in _CLIENT_TOOL_NAMES
                        and call_id not in tool_outputs
                    ):
                        out = execute_tool(name, json.loads(item.get("arguments") or "{}"))
                        tool_outputs[call_id] = out
                        threading.Thread(
                            target=_post_tool_output, args=(call_id, out), daemon=True
                        ).start()
            elif etype == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    text_chunks.append(delta)
            elif etype == "response.completed":
                status = "completed"
                break
            elif etype == "response.failed":
                status = "failed"
                break

    output: list[dict[str, Any]] = list(function_calls.values())
    output += [
        {"type": "function_call_output", "call_id": call_id, "output": out}
        for call_id, out in tool_outputs.items()
    ]
    if text_chunks:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "".join(text_chunks)}],
            }
        )
    return {"status": status, "output": output, "error": None}


def _extract_all_text(body: dict[str, Any]) -> str:
    """
    Concatenate all assistant text from a terminal response body.

    :param body: The terminal response body from GET /v1/responses/{id}.
    :returns: All assistant text blocks joined by newlines, lowercased.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _collect_function_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract all function_call items from a response body.

    :param body: The terminal response body.
    :returns: List of function_call output items.
    """
    return [item for item in body.get("output", []) if item.get("type") == "function_call"]


def _assert_client_tool_output_contains(
    result: dict[str, Any],
    needle: str,
) -> None:
    """
    Assert that at least one client-side tool output contains ``needle``.

    Checks the actual ``function_call_output`` items for client-side
    tools (Glob, Read, Bash, Grep) — not the agent's prose. This
    prevents false-positives from LLM hallucination.

    :param result: The terminal response body.
    :param needle: String that must appear in a tool output.
    """
    function_calls = _collect_function_calls(result)
    client_tools = {"Glob", "Read", "Bash", "Grep"}
    client_call_ids = {fc["call_id"] for fc in function_calls if fc.get("name") in client_tools}
    assert client_call_ids, (
        f"No client-side tool calls found. Called: {[fc.get('name') for fc in function_calls]}"
    )
    outputs = [
        item["output"]
        for item in result.get("output", [])
        if item.get("type") == "function_call_output"
        and item.get("call_id") in client_call_ids
        and item.get("output")
    ]
    assert any(needle in out for out in outputs), (
        f"Expected '{needle}' in client tool output (not just "
        f"agent text). Tool outputs: {[o[:200] for o in outputs]}"
    )


def test_openai_coder_lists_files_with_client_tools(
    live_server: str,
    http_client: httpx.Client,
    openai_coder_agent: str,
    live_runner_id: str,
    sample_code_dir: Path,
) -> None:
    """
    The openai-coder agent uses client-side Glob/Read to list and
    read files, proving client-side tools work alongside codex
    sandbox builtins.

    **What breaks if wrong:** If client-side tools are not
    registered, the agent only sees codex:Shell (sandbox) and
    cannot access the host temp directory. If tunneling fails,
    the agent's client tool call never relays upstream (or errors
    "not in local dispatch table") and the turn never completes.

    :param live_server: Server base URL for the tunneling POST clients.
    :param http_client: HTTP client pointed at the live server.
    :param openai_coder_agent: The uploaded openai-coder agent name.
    :param live_runner_id: Registered runner id for session dispatch.
    :param sample_code_dir: Temp dir with calculator.py, utils.py.
    """
    result = _run_with_tunneling(
        http_client,
        base_url=live_server,
        agent_name=openai_coder_agent,
        runner_id=live_runner_id,
        user_input=f"List all Python files in {sample_code_dir} and tell me "
        f"their names. Use the Glob tool with pattern '**/*.py' and "
        f"path '{sample_code_dir}'. Then use the Read tool to read "
        f"the contents of calculator.py from that directory. "
        f"Do NOT use Shell or ApplyPatch — use the Glob and Read "
        f"tools only.",
    )

    assert result["status"] == "completed", (
        f"Expected completed, got {result['status']}. Error: {result.get('error')}"
    )
    # Assert on TOOL OUTPUT: "calculator" must appear in what the
    # client-side tool returned, not just the agent's summary.
    _assert_client_tool_output_contains(result, "calculator")


def _assert_write_and_read_called(result: dict[str, Any]) -> None:
    """
    Assert that both Write and Read appear in the response's tool calls.

    :param result: The terminal response body.
    """
    called_names = [fc["name"] for fc in _collect_function_calls(result)]
    assert "Write" in called_names, (
        f"Expected Write tool call but got: {called_names}. "
        f"The agent may have used codex:ApplyPatch instead."
    )
    assert "Read" in called_names, (
        f"Expected Read tool call but got: {called_names}. "
        f"The agent may have used codex:Shell 'cat' instead."
    )


def _assert_file_written_locally(
    target_file: Path,
    sentinel: str,
) -> None:
    """
    Assert that a file exists on the local filesystem with expected content.

    Proves client-side Write executed locally, not in the sandbox.

    :param target_file: Path to the file that should exist.
    :param sentinel: String that must appear in the file contents.
    """
    assert target_file.exists(), (
        f"File {target_file} was not created on the local "
        f"filesystem. The Write tool may have executed in the "
        f"codex sandbox instead of locally."
    )
    actual_content = target_file.read_text()
    assert sentinel in actual_content, (
        f"Expected '{sentinel}' in file contents, got: {actual_content[:200]!r}."
    )


def test_openai_coder_writes_and_reads_file(
    live_server: str,
    http_client: httpx.Client,
    openai_coder_agent: str,
    live_runner_id: str,
    tmp_path: Path,
) -> None:
    """
    The openai-coder agent uses client-side Write and Read to
    create a file and verify its contents locally.

    **What breaks if wrong:** If Write/Read fall back to
    codex:ApplyPatch/Shell, the file lands in the sandbox — not
    on the host filesystem. The local file assertion fails.

    :param live_server: Server base URL for the tunneling POST clients.
    :param http_client: HTTP client pointed at the live server.
    :param openai_coder_agent: The uploaded openai-coder agent name.
    :param live_runner_id: Registered runner id for session dispatch.
    :param tmp_path: Pytest-provided temporary directory.
    """
    target_file = tmp_path / "agent_test_output.txt"
    sentinel = "OMNIGENT_E2E_CANARY_2026"

    result = _run_with_tunneling(
        http_client,
        base_url=live_server,
        agent_name=openai_coder_agent,
        runner_id=live_runner_id,
        user_input=f"Do exactly these two steps:\n"
        f"1. Use the Write tool to create a file at "
        f"'{target_file}' with the content '{sentinel}'\n"
        f"2. Use the Read tool to read back the file at "
        f"'{target_file}' and show me its contents.\n"
        f"Use ONLY the Write and Read tools. Do NOT use Shell "
        f"or ApplyPatch.",
    )

    assert result["status"] == "completed", (
        f"Expected completed, got {result['status']}. Error: {result.get('error')}"
    )
    _assert_write_and_read_called(result)
    _assert_file_written_locally(target_file, sentinel)

    all_text = _extract_all_text(result)
    assert sentinel in all_text, (
        f"Expected '{sentinel}' in agent response (from Read tool output), got: {all_text[:500]}"
    )
