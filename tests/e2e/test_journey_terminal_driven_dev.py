"""E2E journey test: terminal-driven development workflow (mock LLM).

Exercises the realistic developer workflow where an agent uses
terminals to run commands, inspect output, and follow up with
commands that build on prior results. Proves that:

1. The agent can create terminals and execute commands.
2. Terminal output flows back through tool outputs.
3. Terminal state (tmux session) persists across conversation
   turns so follow-up commands observe prior side effects.

Skipped if tmux is not installed on the host.

Usage::

    pytest tests/e2e/test_journey_terminal_driven_dev.py -v
"""

from __future__ import annotations

import shutil
import uuid

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

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; terminal journey tests need tmux on PATH",
)


def _get_function_call_outputs(
    client: httpx.Client,
    session_id: str,
    tool_name: str,
) -> list[str]:
    """Return raw outputs of every *tool_name* call in conversation order.

    Walks session items looking for ``function_call`` /
    ``function_call_output`` pairs. Assertions land on deterministic
    tool output strings rather than flaky LLM prose.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    :param tool_name: Only outputs of calls to this tool are returned.
    :returns: Ordered list of raw output strings.
    """
    resp = client.get(f"/v1/sessions/{session_id}/items?limit=200")
    resp.raise_for_status()
    items = resp.json()["data"]

    calls_by_id: dict[str, dict] = {}
    for item in items:
        itype = item.get("type")
        data = item.get("data") or {}
        name = item.get("name") or data.get("name")
        call_id = item.get("call_id") or data.get("call_id")
        if itype == "function_call" and name == tool_name and call_id:
            calls_by_id[call_id] = item

    outputs: list[str] = []
    for item in items:
        itype = item.get("type")
        data = item.get("data") or {}
        call_id = item.get("call_id") or data.get("call_id")
        output = item.get("output") or data.get("output")
        if itype == "function_call_output" and call_id in calls_by_id:
            outputs.append(str(output or ""))
    return outputs


@pytest.mark.llm_flaky(reruns=2)
def test_terminal_multi_command_workflow(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Multi-command developer workflow: run a command, see output,
    follow up with another command that references the first.

    Turn 1: ``echo hello_world`` — mock LLM emits sys_terminal_launch
    then sys_terminal_send(echo hello_world) then sys_terminal_read
    then a text reply. Verify terminal tool was invoked and the output
    contains the marker.

    Turn 2: ``echo goodbye_world`` in the same terminal — mock LLM
    emits sys_terminal_send(echo goodbye_world) then sys_terminal_read
    then a text reply. Verify the second marker appears.
    """
    model = f"mock-terminal-multi-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"terminal-multi-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a terminal test assistant. "
            "Use sys_terminal_launch, sys_terminal_send, and sys_terminal_read "
            "to execute shell commands and report results."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "terminals": {
                "bash": {
                    "command": "bash",
                    "os_env": {"type": "caller_process", "sandbox": {"type": "none"}},
                }
            },
            "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        },
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # ── Turn 1: launch terminal and echo hello_world ──────────
    # Mock: launch -> send echo hello_world -> read -> text reply.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_launch1",
                        "name": "sys_terminal_launch",
                        "arguments": '{"terminal": "bash", "session": "s1"}',
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_send1",
                        "name": "sys_terminal_send",
                        "arguments": (
                            '{"terminal": "bash", "session": "s1",'
                            ' "text": "echo hello_world", "keys": "Enter"}'
                        ),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_read1",
                        "name": "sys_terminal_read",
                        "arguments": '{"terminal": "bash", "session": "s1"}',
                    }
                ],
            },
            {"text": "I ran echo hello_world and the terminal showed hello_world."},
        ],
        key=model,
    )

    resp_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Run `echo hello_world` in a terminal and tell me the output.",
    )
    result_1 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=resp_id_1,
        timeout=180,
    )
    assert result_1["status"] == "completed", (
        f"Turn 1 failed: status={result_1['status']!r}, error={result_1.get('error')!r}"
    )

    # The agent must have used sys_terminal_launch.
    launches_1 = _get_function_call_outputs(http_client, session_id, "sys_terminal_launch")
    assert launches_1, (
        f"sys_terminal_launch was never called in turn 1; "
        f"session_id={session_id}. The agent must launch a terminal "
        f"before it can execute commands."
    )

    # Verify send and read were called (tool pipeline ran end-to-end).
    sends_1 = _get_function_call_outputs(http_client, session_id, "sys_terminal_send")
    reads_1 = _get_function_call_outputs(http_client, session_id, "sys_terminal_read")
    assert sends_1, f"sys_terminal_send was never called in turn 1; session_id={session_id}."
    assert reads_1, f"sys_terminal_read was never called in turn 1; session_id={session_id}."

    # Stronger output assertions: the read output must be non-empty and
    # contain "hello_world" — proving the echo actually ran and tmux
    # captured it before the read call completed.
    combined_reads_1 = " ".join(reads_1)
    assert "hello_world" in combined_reads_1, (
        f"Expected 'hello_world' in sys_terminal_read output from turn 1, "
        f"got reads_1={reads_1!r}. The echo may not have flushed before the "
        f"read, or the terminal send did not execute the command."
    )

    # ── Turn 2: echo goodbye_world ────────────────────────────
    # Mock: reuse existing terminal, send goodbye_world, read, reply.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_send2",
                        "name": "sys_terminal_send",
                        "arguments": (
                            '{"terminal": "bash", "session": "s1",'
                            ' "text": "echo goodbye_world", "keys": "Enter"}'
                        ),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_read2",
                        "name": "sys_terminal_read",
                        "arguments": '{"terminal": "bash", "session": "s1"}',
                    }
                ],
            },
            {"text": "I ran echo goodbye_world and the terminal showed goodbye_world."},
        ],
        key=model,
    )

    resp_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=("Now run `echo goodbye_world` in the same terminal and tell me what you see."),
    )
    result_2 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=resp_id_2,
        timeout=180,
    )
    assert result_2["status"] == "completed", (
        f"Turn 2 failed: status={result_2['status']!r}, error={result_2.get('error')!r}"
    )

    # Verify a second send and read were issued in turn 2.
    sends_2 = _get_function_call_outputs(http_client, session_id, "sys_terminal_send")
    reads_2 = _get_function_call_outputs(http_client, session_id, "sys_terminal_read")
    assert len(sends_2) >= 2, (
        f"Expected at least 2 sys_terminal_send calls across both turns; "
        f"got {len(sends_2)}. Turn 2 send may not have executed."
    )
    assert len(reads_2) >= 2, (
        f"Expected at least 2 sys_terminal_read calls across both turns; "
        f"got {len(reads_2)}. Turn 2 read may not have executed."
    )

    # Stronger output assertion: the combined reads across both turns must
    # contain "goodbye_world" — proving the second echo ran and was captured.
    combined_reads_2 = " ".join(reads_2)
    assert "goodbye_world" in combined_reads_2, (
        f"Expected 'goodbye_world' in sys_terminal_read outputs after turn 2, "
        f"got reads_2={reads_2!r}. The echo may not have flushed before the "
        f"read, or the terminal send did not execute the command."
    )

    # Soft check: the agent should have reused the terminal (only
    # one launch across both turns). If it launched twice, the test
    # still passes but logs a warning — terminal persistence is
    # tested more rigorously in the next test.
    all_launches = _get_function_call_outputs(http_client, session_id, "sys_terminal_launch")
    if len(all_launches) > 1:
        print(
            f"[WARN] Agent launched {len(all_launches)} terminals "
            f"across 2 turns; ideally should reuse one."
        )


@pytest.mark.llm_flaky(reruns=2)
def test_terminal_persists_across_turns(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Terminal state (tmux session) persists between agent turns.

    Turn 1: Create a file via terminal
    (``echo "test content" > /tmp/omni_test_<sid>.txt``).

    Turn 2: Read the file via terminal
    (``cat /tmp/omni_test_<sid>.txt``).

    The second turn's tool output must contain ``test content``,
    proving the tmux session (and its filesystem side effects)
    survived the turn boundary. If per-workflow cleanup kills
    the tmux session, the file would still exist on disk but the
    test also validates that the agent can reuse the terminal
    without relaunching.
    """
    model = f"mock-terminal-persist-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"terminal-persist-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a terminal test assistant. "
            "Use sys_terminal_launch, sys_terminal_send, and sys_terminal_read "
            "to execute shell commands and report results."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "terminals": {
                "bash": {
                    "command": "bash",
                    "os_env": {"type": "caller_process", "sandbox": {"type": "none"}},
                }
            },
            "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        },
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    test_file = f"/tmp/omni_test_{session_id[:8]}.txt"

    # ── Turn 1: create the file ───────────────────────────────
    # Mock: launch -> send echo "test content" > file -> read -> reply.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_launch_p1",
                        "name": "sys_terminal_launch",
                        "arguments": '{"terminal": "bash", "session": "s1"}',
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_send_p1",
                        "name": "sys_terminal_send",
                        "arguments": (
                            f'{{"terminal": "bash", "session": "s1",'
                            f' "text": "echo \\"test content\\" > {test_file}",'
                            f' "keys": "Enter"}}'
                        ),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_read_p1",
                        "name": "sys_terminal_read",
                        "arguments": '{"terminal": "bash", "session": "s1"}',
                    }
                ],
            },
            {"text": f"I created the file at {test_file}. Confirmed it ran."},
        ],
        key=model,
    )

    resp_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(f'Run `echo "test content" > {test_file}` in a terminal. Confirm it ran.'),
    )
    result_1 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=resp_id_1,
        timeout=180,
    )
    assert result_1["status"] == "completed", (
        f"Turn 1 failed: status={result_1['status']!r}, error={result_1.get('error')!r}"
    )

    # Sanity: a terminal was launched.
    launches = _get_function_call_outputs(http_client, session_id, "sys_terminal_launch")
    assert launches, f"No sys_terminal_launch in turn 1; session_id={session_id}."

    # ── Turn 2: read the file ─────────────────────────────────
    # Mock: reuse terminal, send cat file, read, reply.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_send_p2",
                        "name": "sys_terminal_send",
                        "arguments": (
                            f'{{"terminal": "bash", "session": "s1",'
                            f' "text": "cat {test_file}", "keys": "Enter"}}'
                        ),
                    }
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_read_p2",
                        "name": "sys_terminal_read",
                        "arguments": '{"terminal": "bash", "session": "s1"}',
                    }
                ],
            },
            {"text": "The file contains: test content"},
        ],
        key=model,
    )

    resp_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Now run `cat {test_file}` in the same terminal and tell me what the file contains."
        ),
    )
    result_2 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=resp_id_2,
        timeout=180,
    )
    assert result_2["status"] == "completed", (
        f"Turn 2 failed: status={result_2['status']!r}, error={result_2.get('error')!r}"
    )

    # The read output must contain "test content".
    reads = _get_function_call_outputs(http_client, session_id, "sys_terminal_read")
    combined_reads = " ".join(reads)
    assert "test content" in combined_reads, (
        f"Expected 'test content' in sys_terminal_read output "
        f"from turn 2, got reads={reads!r}. If the file exists "
        f"but the read is empty, the terminal session was torn "
        f"down between turns and the agent had to relaunch — "
        f"check whether cleanup_conversation regressed into the "
        f"workflow's finally block."
    )

    # The agent should not have needed to relaunch — one launch
    # total across both turns.
    all_launches = _get_function_call_outputs(http_client, session_id, "sys_terminal_launch")
    assert len(all_launches) == 1, (
        f"Expected exactly 1 sys_terminal_launch across both "
        f"turns, got {len(all_launches)}. If >1, the terminal "
        f"session was destroyed between turns and the agent had "
        f"to relaunch — confirms a per-workflow cleanup regression."
    )
