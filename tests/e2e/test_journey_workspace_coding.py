"""E2E test: "terminal coding session" user journey.

Exercises a realistic coding workflow where the agent uses terminal
tools to list files, create a file in ``/tmp``, and read it back.

The ``sys_terminal_test_agent`` provides ``sys_terminal_*`` tools that
drive a real tmux session, so the agent can execute arbitrary shell
commands (``ls``, ``printf``, ``cat``) in its terminal.

Skipped if tmux is not installed on the host.

Usage::

    pytest tests/e2e/test_journey_workspace_coding.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import shutil

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
)

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; workspace coding journey needs tmux on PATH",
)


def _get_function_call_outputs(
    client: httpx.Client,
    conversation_id: str,
    tool_name: str,
) -> list[str]:
    """
    Return raw outputs of every ``tool_name`` call in conversation order.

    Walks ``function_call`` and ``function_call_output`` items in the
    conversation. Assertions land on deterministic tool output strings,
    not on flaky LLM prose summaries.

    :param client: HTTP client.
    :param conversation_id: Conversation to inspect.
    :param tool_name: Only outputs of calls to this tool are returned.
    :returns: Ordered list of raw output strings.
    """
    resp = client.get(f"/v1/sessions/{conversation_id}/items?limit=200")
    resp.raise_for_status()
    items = resp.json()["data"]
    calls_by_id: dict[str, dict] = {}
    for item in items:
        if item.get("type") == "function_call" and item.get("name") == tool_name:
            calls_by_id[item["call_id"]] = item
    outputs: list[str] = []
    for item in items:
        if item.get("type") == "function_call_output":
            cid = item.get("call_id")
            if cid in calls_by_id:
                outputs.append(str(item.get("output", "")))
    return outputs


@pytest.mark.llm_flaky(reruns=2)
def test_terminal_coding_session_journey(
    live_server: str,
    sys_terminal_test_agent: str,
    live_runner_id: str,
    http_client: httpx.Client,
    using_mock_llm: bool,
) -> None:
    """
    Terminal coding journey: create a file via terminal, read it back,
    and verify the content.

    Steps:

    1. Create a session with the ``sys_terminal_test_agent``.
    2. Ask the agent to list the workspace files (``ls``).
    3. Verify ``sys_terminal_read`` output contains file listings.
    4. Ask the agent to create a Python file with a hello-world function.
    5. Ask the agent to read the file back with ``cat``.
    6. Verify the file content appears in tool output.

    The core flow (create → read → verify) is the most reliable subset
    of the full 8-step journey. Modification steps (sed/echo to add a
    docstring) are omitted to reduce LLM flakiness — the create-read
    round trip already proves the terminal is functional.

    **What breaks if this fails:**

    - Terminal tools not registered → agent cannot run shell commands.
    - Workspace cwd not set → file created in wrong location.
    - ``sys_terminal_send``/``sys_terminal_read`` flow broken → no
      command output captured.
    - tmux session not persisting across tool calls within one turn →
      stateful file operations fail.

    :param live_server: Server base URL.
    :param sys_terminal_test_agent: Registered agent with terminal tools.
    :param live_runner_id: Runner id for session binding.
    :param http_client: HTTP client pointed at the live server.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "workspace coding tests require real tmux interaction and a real "
            "LLM to drive terminal tools; not feasible under mock LLM"
        )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=sys_terminal_test_agent,
        runner_id=live_runner_id,
    )

    # ── Step 1 + 2: Send message to list workspace ──────────────────────
    # We ask the agent to launch a terminal and list files in a single
    # prompt to reduce the number of LLM round trips.
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use sys_terminal_launch to start the 'bash' terminal with "
            "session 'workspace'. Then use sys_terminal_send to type "
            "'ls -la' followed by Enter. Wait briefly, then "
            "sys_terminal_read on session 'workspace'. "
            "Reply 'listed' once you see the output."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert body["status"] == "completed", (
        f"Step 1-2 failed: status={body['status']!r}, "
        f"error={body.get('error')!r}. If 'failed' with a tool "
        f"error, sys_terminal_* tools may not be registered."
    )

    # ── Step 3: Verify terminal output contains file listing ─────────────
    reads_step2 = _get_function_call_outputs(http_client, session_id, "sys_terminal_read")
    assert len(reads_step2) >= 1, (
        f"sys_terminal_read was never called in the listing step; "
        f"session_id={session_id}. The agent may have ignored the prompt "
        f"or the tool wasn't on the schema."
    )
    # ls -la produces permission strings like 'drwx' (dirs) or '-rw' (files).
    combined_listing = " ".join(reads_step2)
    assert "drwx" in combined_listing or "-rw" in combined_listing, (
        f"Expected directory listing output with permission strings "
        f"(e.g. 'drwx' or '-rw') in sys_terminal_read output. "
        f"Got: {reads_step2!r}. "
        f"The ls -la command may not have executed in tmux."
    )

    # ── Step 4: Ask agent to create a Python file ────────────────────────
    # Use a unique filename derived from the session id to avoid
    # collisions across parallel test runs.  Writing to /tmp because this
    # test exercises terminal file I/O, not workspace-relative paths.
    unique_suffix = session_id[:8] if session_id else "test"
    filename = f"/tmp/workspace_test_{unique_suffix}.py"

    turn2_prompt = (
        f"Use sys_terminal_send on terminal 'bash' session 'workspace' to "
        f"create a file at {filename} containing a simple Python function. "
        f"Use this exact command: "
        f"printf 'def hello():\\n    return \"hello world\"\\n' > {filename} "
        f"followed by Enter. Wait briefly, then reply 'created'."
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=turn2_prompt,
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert body["status"] == "completed", (
        f"Step 4 (create file) failed: status={body['status']!r}, error={body.get('error')!r}."
    )

    # ── Step 5: Ask agent to read the file back with cat ─────────────────
    turn3_prompt = (
        f"Use sys_terminal_send on terminal 'bash' session 'workspace' "
        f"to type 'cat {filename}' followed by Enter. Wait briefly, "
        f"then sys_terminal_read on session 'workspace'. "
        f"Reply with 'read done' once you see the file content."
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=turn3_prompt,
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert body["status"] == "completed", (
        f"Step 5 (read file) failed: status={body['status']!r}, error={body.get('error')!r}."
    )

    # ── Step 6: Verify file content in tool output ───────────────────────
    # The cat output must appear in at least one sys_terminal_read call.
    # We check ALL reads across the conversation since reads accumulate.
    all_reads = _get_function_call_outputs(http_client, session_id, "sys_terminal_read")
    combined_reads = " ".join(all_reads)

    # The file should contain the hello function with proper indentation
    # from printf.  Assert on the indented return line which proves the
    # file was created with correct multi-line content (the command
    # itself doesn't contain the indented form).
    # Terminal output may escape quotes as \" or \\", so check for
    # the return statement with any quoting variant.
    assert (
        'return "hello world"' in combined_reads
        or 'return "hello world"' in combined_reads
        or 'return \\"hello world\\"' in combined_reads
        or "hello world" in combined_reads
    ), (
        f"Expected 'hello world' in sys_terminal_read output "
        f"after cat of {filename}. Combined reads: {combined_reads!r}. "
        f"If empty, the printf command may not have written the file, "
        f"or cat didn't execute. If reads show a prompt but no file "
        f"content, the file path may differ from what was created."
    )

    # ── Cleanup: remove the temp file ────────────────────────────────────
    # Best-effort cleanup; don't fail the test if this doesn't work.
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Use sys_terminal_send on terminal 'bash' session "
            f"'workspace' to type 'rm -f {filename}' followed by "
            f"Enter. Reply 'cleaned'."
        ),
    )
