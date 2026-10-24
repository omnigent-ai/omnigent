"""A real host reports forkserver loss without inventing a runner exit status."""

from __future__ import annotations

import contextlib
import os
import re
import signal
from pathlib import Path

import httpx
import psutil
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.server_runner import server_runner
from tests._helpers.zerobus_capture import ZeroBusCapture
from tests.e2e.conftest import configure_mock_llm, lookup_agent_id, register_inline_agent
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import (
    _gate_pending,
    _poll_until,
    _send_user_message,
    _session_snapshot,
)

pytestmark = pytest.mark.posix_only


@pytest.mark.timeout(150)
def test_host_ships_lost_status_evidence_for_a_real_forked_runner(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    model = "zygote-loss-mock"
    configure_mock_llm(
        isolated_mock_llm_server_url, [{"text": "HELD_TURN", "block": True}], key=model
    )
    with (
        contextlib.closing(ZeroBusCapture()) as capture,
        server_runner(
            tmp_path / "stack",
            base_env={
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "LANG", "LC_ALL"}
            },
        ) as stack,
    ):
        stack.start_host(
            env={
                "OMNIGENT_RUNNER_ZYGOTE": "1",
                "OPENAI_API_KEY": "mock-key",
                "OPENAI_BASE_URL": f"{isolated_mock_llm_server_url}/v1",
                "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
                "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
                "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
                "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
            }
        )
        assert stack.host is not None
        with httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as client:
            _poll_until(
                lambda: bool(client.get("/v1/hosts").json()["hosts"]),
                timeout=30,
                what="host registration",
            )
            [host] = client.get("/v1/hosts").json()["hosts"]
            name = register_inline_agent(
                client,
                name=model,
                harness="openai-agents",
                model=model,
                profile="",
                prompt="Return a brief answer.",
                mock_llm_base_url=f"{isolated_mock_llm_server_url}/v1",
            )
            created = client.post(
                "/v1/sessions",
                json={
                    "agent_id": lookup_agent_id(client, name),
                    "host_id": host["host_id"],
                    "workspace": str(stack.workspace),
                },
            )
            created.raise_for_status()
            session = created.json()["id"]
            runner_id = _session_snapshot(client, session)["runner_id"]
            _send_user_message(client, session)
            _poll_until(
                lambda: _gate_pending(isolated_mock_llm_server_url),
                timeout=40,
                what="a real in-flight model turn",
            )

            def forked_runner() -> tuple[psutil.Process, psutil.Process] | None:
                pattern = rf"Launched runner {re.escape(runner_id)} .*?\(pid=(\d+)\)"
                for path in (stack.runner_home / ".omnigent" / "logs" / "host").glob("*.log"):
                    for pid in re.findall(pattern, path.read_text()):
                        with contextlib.suppress(psutil.NoSuchProcess, psutil.ZombieProcess):
                            child = psutil.Process(int(pid))
                            parent = child.parent()
                            if (
                                parent is not None
                                and parent.ppid() == stack.host.pid
                                and "omnigent.runner._zygote" in parent.cmdline()
                            ):
                                return parent, child
                return None

            _poll_until(
                lambda: forked_runner() is not None,
                timeout=15,
                what="the session's owned forkserver and runner",
            )
            processes = forked_runner()
            assert processes is not None, stack.log_tail()
            parent, child = processes
            parent.kill()
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()

            def evidence() -> list[dict]:
                return [
                    row
                    for row in capture.rows()
                    if row.get("event_name") == "runner_zygote_status_unavailable"
                    and row.get("session_id") == session
                ]

            _poll_until(
                lambda: any(row["attributes"].get("child_alive") == "False" for row in evidence()),
                timeout=30,
                what="the shipped unknown-exit-status diagnostic",
            )
            rows = evidence()
            assert 1 <= len(rows) <= 2
            attrs = rows[-1]["attributes"]
            assert attrs["runner_id"] == runner_id
            assert attrs["runner_pid"] == str(child.pid)
            assert attrs["zygote_pid"] == str(parent.pid)
            assert attrs["zygote_returncode"] == str(-signal.SIGKILL)
            assert attrs["zygote_signal"] == str(signal.SIGKILL)
            assert attrs["reported_runner_exit_code"] == "254"
            assert attrs["runner_exit_code_known"] == "False"
            _poll_until(
                lambda: _session_snapshot(client, session)["status"] == "failed",
                timeout=40,
                what="preserved active-turn failure",
            )
            assert _session_snapshot(client, session)["last_task_error"] is not None
