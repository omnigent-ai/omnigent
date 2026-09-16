"""Multi-turn prompts using real Linux sandboxes and an inherited terminal.

Run without credentials for scripted model responses, or with --llm-api-key
for a live model. Filesystem assertions use actual tool results and host files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.test_journey_workspace_coding import _get_function_call_outputs

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux OverlayFS required")


def test_copy_on_write_shared_across_prompts_and_terminal(
    http_client,
    live_runner_id,
    mock_llm_server_url,
    using_mock_llm,
    tmp_path,
    request,
):
    bwrap = shutil.which("bwrap")
    if not bwrap or "--tmp-overlay" not in subprocess.check_output([bwrap, "--help"], text=True):
        pytest.skip("Bubblewrap 0.11+ required")
    if not shutil.which("tmux"):
        pytest.skip("tmux required")
    deps = tmp_path / "dependencies"
    deps.mkdir()
    (deps / "original").write_text("original")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    model = f"mock-cow-{uuid.uuid4().hex[:8]}" if using_mock_llm else "gpt-5.4"
    name = register_inline_agent(
        http_client,
        name=f"cow-{uuid.uuid4().hex[:8]}",
        harness="openai-agents",
        model=model,
        profile=request.config.getoption("--profile") or "",
        prompt="Follow each requested tool operation exactly and report its result.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1" if using_mock_llm else None,
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": str(tmp_path),
                "sandbox": {
                    "type": "linux_bwrap",
                    "allow_network": False,
                    "read_paths": [str(Path(__file__).resolve().parents[2])],
                    "write_paths": [str(artifacts), {"path": str(deps), "copy_on_write": True}],
                },
            },
            "terminals": {"bash": {"command": "bash", "os_env": "inherit"}},
        },
    )
    session = create_runner_bound_session(http_client, agent_name=name, runner_id=live_runner_id)

    def turn(prompt, calls):
        before = http_client.get(f"/v1/sessions/{session}")
        before.raise_for_status()
        previous_items = {item["id"] for item in before.json().get("items", [])}
        if using_mock_llm:
            responses = [
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_{uuid.uuid4().hex[:8]}",
                            "name": tool,
                            "arguments": json.dumps(arguments),
                        }
                    ]
                }
                for tool, arguments in calls
            ]
            configure_mock_llm(mock_llm_server_url, [*responses, {"text": "Done."}], key=model)
        send_user_message_to_session(http_client, session_id=session, content=prompt)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = http_client.get(f"/v1/sessions/{session}")
            response.raise_for_status()
            snapshot = response.json()
            assert snapshot.get("status") != "failed", snapshot.get("last_task_error")
            new_items = [
                {**item, **(item.get("data") or {})}
                for item in snapshot.get("items", [])
                if item["id"] not in previous_items
            ]
            # A queued turn may still be idle with the previous turn's output.
            if snapshot.get("status") == "idle" and any(
                item.get("type") == "message"
                and item.get("role") == "assistant"
                and item.get("status") == "completed"
                for item in new_items
            ):
                assert (deps / "original").read_text() == "original"
                assert not (deps / "from-terminal").exists()
                return
            time.sleep(0.5)
        pytest.fail("Copy-on-write turn did not produce a new completed assistant message")

    turn(
        f"Use sys_os_write to set {deps}/original to exactly 'tool edit'. "
        "Then launch the inherited bash terminal with session 'shared'.",
        [
            ("sys_os_write", {"path": str(deps / "original"), "content": "tool edit"}),
            ("sys_terminal_launch", {"terminal": "bash", "session": "shared"}),
        ],
    )
    command = f"cat {deps}/original > {artifacts}/seen; echo terminal-edit > {deps}/from-terminal"
    turn(
        f"Send this command to the existing bash terminal session 'shared': {command}. "
        f"Then use sys_os_shell to run: sleep 1; cat {deps}/from-terminal",
        [
            ("sys_terminal_send", {"terminal": "bash", "session": "shared", "text": command}),
            ("sys_os_shell", {"command": f"sleep 1; cat {deps}/from-terminal"}),
        ],
    )
    assert (artifacts / "seen").read_text() == "tool edit"
    assert (deps / "original").read_text() == "original"
    assert not (deps / "from-terminal").exists()
    outputs = _get_function_call_outputs(http_client, session, "sys_os_shell")
    assert any("terminal-edit" in output for output in outputs), outputs
    turn(
        f"Close bash terminal session 'shared'. Then use sys_os_read to read {deps}/original. "
        "It should still contain the earlier edit.",
        [
            ("sys_terminal_close", {"terminal": "bash", "session": "shared"}),
            ("sys_os_read", {"path": str(deps / "original")}),
        ],
    )
    outputs = _get_function_call_outputs(http_client, session, "sys_os_read")
    assert any("tool edit" in output for output in outputs), outputs
