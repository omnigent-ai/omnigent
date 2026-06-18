"""E2E journey test: terminal-driven development workflow.

Exercises the realistic developer workflow where an agent uses
terminals to run commands, inspect output, and follow up with
commands that build on prior results. Proves that:

1. The agent can create terminals and execute commands.
2. Terminal output flows back through tool outputs.
3. Terminal state (tmux session) persists across conversation
   turns so follow-up commands observe prior side effects.

Skipped if tmux is not installed on the host.

Usage::

    pytest tests/e2e/test_journey_terminal_driven_dev.py \\
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
    live_server: str,
    sys_terminal_test_agent: str,
    http_client: httpx.Client,
    live_runner_id: str,
) -> None:
    """Multi-command developer workflow: run a command, see output,
    follow up with another command that references the first.

    Turn 1: ``echo hello_world`` — verify terminal tool was invoked
    and the output contains the marker.

    Turn 2: ``echo goodbye_world`` in the same terminal — verify
    the second marker appears and ideally the same terminal was
    reused (no second launch).
    """
    session_id = create_runner_bound_session(
        http_client,
        agent_name=sys_terminal_test_agent,
        runner_id=live_runner_id,
    )

    # ── Turn 1: echo hello_world ──────────────────────────────
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

    # The agent must have used sys_terminal_send (or at minimum
    # sys_terminal_launch). Check that the tool output contains
    # the marker.
    sends_1 = _get_function_call_outputs(http_client, session_id, "sys_terminal_send")
    reads_1 = _get_function_call_outputs(http_client, session_id, "sys_terminal_read")
    launches_1 = _get_function_call_outputs(http_client, session_id, "sys_terminal_launch")

    assert launches_1, (
        f"sys_terminal_launch was never called in turn 1; "
        f"session_id={session_id}. The agent must launch a terminal "
        f"before it can execute commands."
    )

    # hello_world must appear in send or read outputs.
    all_outputs_1 = " ".join(sends_1 + reads_1)
    assert "hello_world" in all_outputs_1, (
        f"'hello_world' not found in terminal tool outputs after "
        f"turn 1. Sends: {sends_1!r}, Reads: {reads_1!r}. The "
        f"echo command either wasn't sent or the read didn't "
        f"capture the output."
    )

    # ── Turn 2: echo goodbye_world ────────────────────────────
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

    # goodbye_world must appear in the cumulative tool outputs.
    sends_2 = _get_function_call_outputs(http_client, session_id, "sys_terminal_send")
    reads_2 = _get_function_call_outputs(http_client, session_id, "sys_terminal_read")
    all_outputs_2 = " ".join(sends_2 + reads_2)
    assert "goodbye_world" in all_outputs_2, (
        f"'goodbye_world' not found in terminal tool outputs after "
        f"turn 2. Sends: {sends_2!r}, Reads: {reads_2!r}."
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
    live_server: str,
    sys_terminal_test_agent: str,
    http_client: httpx.Client,
    live_runner_id: str,
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
    session_id = create_runner_bound_session(
        http_client,
        agent_name=sys_terminal_test_agent,
        runner_id=live_runner_id,
    )

    test_file = f"/tmp/omni_test_{session_id[:8]}.txt"

    # ── Turn 1: create the file ───────────────────────────────
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
