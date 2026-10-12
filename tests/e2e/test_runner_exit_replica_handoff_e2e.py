"""Real host exits reach a replica with no local runner connection history."""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import httpx
import psutil
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.server_runner import ServerRunner, server_runner
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    register_inline_agent,
    release_mock_gate,
)
from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.proxy import FaultProxy, LoopThread, host_link
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import (
    _gate_pending,
    _poll_until,
    _send_user_message,
    _session_blob,
    _session_snapshot,
)

pytestmark = [
    pytest.mark.timeout(240),
    pytest.mark.min_server_version("0.18.0"),
    pytest.mark.min_runner_version("0.18.0"),
]
_REPO = Path(__file__).resolve().parents[2]
_SERVER_BOOTSTRAP = """
import os
import sys
sys.argv.extend(["--config", os.environ["E2E_SERVER_CONFIG"]])
from omnigent.cli import main
main()
"""


def _base_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}


def _config(root: Path) -> dict[str, str]:
    config = root / "config"
    config.mkdir()
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "debug_router_modules": ["tests._helpers.runner_lifecycle_controls"],
                "runner": {"idle_timeout_s": 0},
            }
        )
    )
    return {
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_RUNNER_ZYGOTE": "0",
        "E2E_SERVER_CONFIG": str(config / "config.yaml"),
    }


def _server_log(stack: ServerRunner) -> str:
    return "\n".join(
        path.read_text(errors="replace")
        for path in (stack.server_home / ".omnigent" / "logs" / "server").glob("*.log")
    )


def _state(client: httpx.Client, runner_id: str, host_id: str = "") -> dict:
    response = client.get(f"/__runner_lifecycle/state/{runner_id}", params={"host_id": host_id})
    response.raise_for_status()
    return response.json()


def _child_runner(stack: ServerRunner) -> psutil.Process | None:
    assert stack.host is not None
    for child in psutil.Process(stack.host.pid).children(recursive=True):
        try:
            if "omnigent.runner._entry" in child.cmdline():
                return child
        except psutil.NoSuchProcess:
            continue
    return None


@pytest.mark.parametrize("phase", ["idle", "active", "never_connected"])
def test_host_report_after_handoff_preserves_only_completed_turns(
    isolated_mock_llm_server_url: str, tmp_path: Path, phase: str
) -> None:
    """Move only the host socket; its actual runner exits on the other replica."""
    mock_url = isolated_mock_llm_server_url
    env = _config(tmp_path)
    answer = f"EXIT_REPORT_COMPLETED_{uuid.uuid4().hex}"
    model = f"exit-report-{uuid.uuid4().hex}"
    configure_mock_llm(mock_url, [{"text": answer, "block": phase == "active"}], key=model)
    timeline = EventLog(tmp_path / "network.jsonl")
    with ExitStack() as resources:
        a = resources.enter_context(
            server_runner(
                tmp_path / "a",
                server_cwd=_REPO,
                server_bootstrap=_SERVER_BOOTSTRAP,
                base_env=_base_env(),
                server_env=env,
            )
        )
        b = resources.enter_context(
            server_runner(
                tmp_path / "b",
                server_cwd=_REPO,
                server_bootstrap=_SERVER_BOOTSTRAP,
                base_env=_base_env(),
                server_env=env,
                database_uri=a.database_uri,
                artifact_location=a.artifact_location,
            )
        )
        loop = LoopThread("exit-report-network")
        loop.start()
        resources.callback(loop.stop)
        proxy = FaultProxy(
            "host", ("127.0.0.1", a.port), loop=loop, events=timeline, classify=host_link
        )
        proxy.start()
        resources.callback(proxy.stop)
        headers = {
            "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
            "x-omnigent-background-session-titles": "off",
        }
        ca = resources.enter_context(
            httpx.Client(base_url=a.base_url, headers=headers, trust_env=False, timeout=100)
        )
        cb = resources.enter_context(
            httpx.Client(base_url=b.base_url, headers=headers, trust_env=False, timeout=100)
        )
        a.host = a._spawn(
            "host",
            ["-m", "omnigent.host._daemon_entry", "--server", proxy.url],
            a.runner_home,
            {**env, "OPENAI_API_KEY": "mock-key", "OPENAI_BASE_URL": f"{mock_url}/v1"},
            cwd=_REPO,
        )
        _poll_until(
            lambda: bool(ca.get("/v1/hosts").json()["hosts"]), timeout=30, what="host registration"
        )
        [host] = ca.get("/v1/hosts").json()["hosts"]
        host_id = host["host_id"]
        name = register_inline_agent(
            ca,
            name=model,
            harness="openai-agents",
            model=model,
            profile="",
            prompt="Return the configured answer.",
            mock_llm_base_url=f"{mock_url}/v1",
        )
        payload = {
            "agent_id": lookup_agent_id(ca, name),
            "host_id": host_id,
            "workspace": str(a.workspace),
        }
        refusal = proxy.refuse({"runner.tunnel"}) if phase == "never_connected" else None
        if refusal is not None:
            resources.callback(refusal.clear)
        pool = resources.enter_context(ThreadPoolExecutor(max_workers=1))
        pending_create = pool.submit(ca.post, "/v1/sessions", json=payload)
        _poll_until(
            lambda: _child_runner(a) is not None, timeout=45, what="the host's real runner child"
        )
        child = _child_runner(a)
        assert child is not None
        if phase == "never_connected":
            child.kill()
        created = pending_create.result(timeout=80)
        created.raise_for_status()
        session_id = created.json()["id"]
        runner_id = _session_snapshot(ca, session_id)["runner_id"]
        assert runner_id
        if phase != "never_connected":
            _send_user_message(ca, session_id)
            if phase == "active":
                _poll_until(
                    lambda: _gate_pending(mock_url), timeout=45, what="actual in-flight model turn"
                )
            else:
                _poll_until(
                    lambda: answer in _session_blob(ca, session_id),
                    timeout=45,
                    what="completed real turn",
                )
                _poll_until(
                    lambda: _session_snapshot(ca, session_id)["status"] == "idle",
                    timeout=15,
                    what="idle session",
                )
            assert _state(ca, runner_id)["runner_local"] is True
            proxy.upstream = ("127.0.0.1", b.port)
            assert proxy.reset({"host.tunnel"}) == 1
            _poll_until(
                lambda: _state(cb, runner_id, host_id)["host_local"],
                timeout=30,
                what="host handoff to B",
            )
            before = _state(cb, runner_id, host_id)
            assert before["runner_local"] is False and before["last_liveness_stamp"] is None, (
                before
            )
            assert _state(ca, runner_id)["runner_local"] is True
            timeline.emit(
                "test", "kill_owned_runner", pid=child.pid, session_id=session_id, phase=phase
            )
            child.kill()
        _poll_until(
            lambda: (
                "reported runner" in _server_log(b)
                if phase != "never_connected"
                else "reported runner" in _server_log(a)
            ),
            timeout=30,
            what="actual host.runner_exited report",
        )
        reader = cb if phase != "never_connected" else ca
        expected = "idle" if phase == "idle" else "failed"
        _poll_until(
            lambda: _session_snapshot(reader, session_id)["status"] == expected,
            timeout=15,
            what=f"reload status {expected}",
        )
        snapshots = [_session_snapshot(reader, session_id) for _ in range(2)]
        (tmp_path / "snapshots.json").write_text(json.dumps(snapshots, indent=2))
        for snapshot in snapshots:
            assert snapshot["status"] == expected
            if phase == "idle":
                assert snapshot.get("last_task_error") is None
                assert answer in json.dumps(snapshot["items"])
            else:
                assert snapshot["last_task_error"]["code"] == "runner_failed_to_start"
        if phase == "active":
            release_mock_gate(mock_url)
