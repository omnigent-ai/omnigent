"""A scheduled transport renewal preserves a real in-flight tool and turn."""

from __future__ import annotations

import json
import textwrap
import uuid
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_dir_agent_with_mock_llm,
    reset_mock_llm,
)
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import (
    _FAILURE_SIGNATURE,
    _poll_until,
    _ReconnectStack,
    _send_user_message,
    _session_blob,
    _session_snapshot,
)

pytestmark = [pytest.mark.timeout(300, method="signal")]


def _write_gated_tool_agent(directory: Path) -> None:
    tools = directory / "tools" / "python"
    tools.mkdir(parents=True)
    (directory / "config.yaml").write_text(
        textwrap.dedent("""\
            spec_version: 1
            name: renewal-test
            executor:
              type: omnigent
              config:
                harness: openai-agents
            prompt: Call the requested tool and report its result.
            os_env:
              type: caller_process
              cwd: .
            """)
    )
    (tools / "gated_increment.py").write_text(
        textwrap.dedent('''\
            import asyncio
            from pathlib import Path
            from omnigent_client.tools import tool

            @tool
            async def gated_increment(counter_path: str, release_path: str) -> str:
                """Record one execution and wait for the test to release it."""
                with Path(counter_path).open("a") as counter:
                    counter.write("called\\n")
                async with asyncio.timeout(240):
                    while not Path(release_path).exists():
                        await asyncio.sleep(0.05)
                return "TOOL_FINISHED_AFTER_RENEWAL"
            ''')
    )


@pytest.mark.parametrize("other_replica", [False, True], ids=["same-replica", "other-replica"])
def test_scheduled_renewal_preserves_an_inflight_tool_and_session(
    mock_llm_server_url: str,
    tmp_path: Path,
    other_replica: bool,
) -> None:
    from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S

    # The old replica must reconcile this renewal before another is scheduled.
    renewal_interval_s = RUNNER_DISCONNECT_GRACE_S + 30 if other_replica else 20
    stack = _ReconnectStack(
        mock_llm_server_url,
        tmp_path,
        runner_environment={
            "OMNIGENT_RUNNER_TUNNEL_RENEWAL_S": str(renewal_interval_s),
            "OMNIGENT_DATA_DIR": str(tmp_path / "runner-data"),
            "OMNIGENT_PROCESS_LOG_FILE": str(tmp_path / "runner-process.log"),
        },
    )
    replica = None
    release = tmp_path / "release-tool"
    counter = tmp_path / "tool-executions"
    try:
        stack.start()
        assert stack.proxy is not None and stack._runner_proc is not None
        runner_pid = stack._runner_proc.pid
        if other_replica:
            replica = stack.start_second_replica()
        reset_mock_llm(mock_llm_server_url)
        model = f"tunnel-renewal-{uuid.uuid4().hex[:8]}"
        answer = f"RENEWAL_COMPLETED_{uuid.uuid4().hex}"
        follow_up = f"RENEWAL_FOLLOWUP_{uuid.uuid4().hex}"
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "renewal_counter_call",
                            "name": "gated_increment",
                            "arguments": json.dumps(
                                {"counter_path": str(counter), "release_path": str(release)}
                            ),
                        }
                    ]
                },
                {"text": answer},
                {"text": follow_up},
            ],
            key=model,
        )
        agent_dir = tmp_path / "agent"
        _write_gated_tool_agent(agent_dir)
        agent_name = register_dir_agent_with_mock_llm(
            stack.client,
            agent_dir=agent_dir,
            name=f"renewal-{uuid.uuid4().hex[:8]}",
            model=model,
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        session_id = create_runner_bound_session(
            stack.client, agent_name=agent_name, runner_id=stack.runner_id
        )
        _send_user_message(stack.client, session_id, "Run the counter once and wait for release.")
        _poll_until(counter.exists, timeout=60, what="the actual tool to start executing")

        before_renewal = len(stack.process_log.read_text())
        if replica is not None:
            # Retarget only future connections; the healthy old socket stays up.
            stack.proxy.retarget("127.0.0.1", replica.port)
        _poll_until(
            lambda: "scheduled tunnel renewal" in stack.process_log.read_text()[before_renewal:],
            timeout=renewal_interval_s + 30,
            what="the runner to close its own tunnel for scheduled renewal",
        )
        client = replica.client if replica is not None else stack.client
        if replica is not None:
            _poll_until(replica.runner_online, timeout=30, what="the runner to register on B")
            recovered = f"Relay: runner transport lost for session={session_id} (live_elsewhere)"
            _poll_until(
                lambda: (
                    recovered in stack.process_log.read_text()
                    or bool(_FAILURE_SIGNATURE.search(stack.process_log.read_text()))
                ),
                timeout=RUNNER_DISCONNECT_GRACE_S + 30,
                what="the old replica to reconcile the still-running session",
            )
            assert recovered in stack.process_log.read_text()
        else:
            _poll_until(
                lambda: (
                    f"Runner {stack.runner_id} connected "
                    in stack.process_log.read_text()[before_renewal:]
                ),
                timeout=30,
                what="the renewed tunnel to register on the same replica",
            )
            stack.wait_runner_online()
        assert not _FAILURE_SIGNATURE.search(stack.process_log.read_text())
        assert counter.read_text() == "called\n"
        assert _session_snapshot(client, session_id).get("status") == "running"

        release.touch()
        _poll_until(
            lambda: answer in _session_blob(client, session_id),
            timeout=60,
            what="the original tool and turn to finish after renewal",
        )
        assert "TOOL_FINISHED_AFTER_RENEWAL" in _session_blob(client, session_id)
        _send_user_message(client, session_id, "Continue in the same session.")
        _poll_until(
            lambda: follow_up in _session_blob(client, session_id),
            timeout=60,
            what="a follow-up turn to complete without restarting the runner",
        )
        assert counter.read_text() == "called\n", "renewal repeated the tool's side effect"
        assert stack._runner_proc.pid == runner_pid and stack._runner_proc.poll() is None
        snapshot = _session_snapshot(client, session_id)
        assert snapshot.get("status") != "failed"
        assert "runner_disconnected" not in json.dumps(snapshot)
        assert not _FAILURE_SIGNATURE.search(stack.process_log.read_text())
        if replica is not None:
            assert not _FAILURE_SIGNATURE.search(replica.process_log.read_text())
    finally:
        release.touch()
        if replica is not None:
            replica.teardown()
        stack.teardown()
