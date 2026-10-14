"""Server-directed retirement preserves a real turn through a slow replica handoff."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.server_runner import ServerRunner, server_runner
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
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
    pytest.mark.timeout(360),
    pytest.mark.min_server_version("0.18.0"),
]
_REPO = Path(__file__).resolve().parents[2]
_SERVER_BOOTSTRAP = """
import os
import sys
sys.argv.extend(["--config", os.environ["E2E_SERVER_CONFIG"]])
from omnigent.cli import main
main()
"""


def _server_log(stack: ServerRunner) -> str:
    return "\n".join(
        path.read_text(errors="replace")
        for path in (stack.server_home / ".omnigent" / "logs" / "server").glob("*.log")
    )


def _state(client: httpx.Client, runner_id: str) -> dict:
    response = client.get(f"/__runner_lifecycle/state/{runner_id}")
    response.raise_for_status()
    return response.json()


@pytest.mark.parametrize("outcome", ["handoff", "retired_dead", "ordinary_dead"])
def test_server_retirement_uses_its_bounded_window_only(
    isolated_mock_llm_server_url: str, tmp_path: Path, outcome: str
) -> None:
    """Use production 90/180-second deadlines with actual HTTP and WebSockets."""
    mock_url = isolated_mock_llm_server_url
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "debug_router_modules": ["tests._helpers.runner_lifecycle_controls"],
                "runner": {"idle_timeout_s": 0},
            }
        )
    )
    env = {"OMNIGENT_CONFIG_HOME": str(config), "E2E_SERVER_CONFIG": str(config / "config.yaml")}
    base_env = {
        key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}
    }
    model = f"retirement-{uuid.uuid4().hex}"
    answer = f"RETIREMENT_COMPLETED_{uuid.uuid4().hex}"
    followup = f"RETIREMENT_FOLLOWUP_{uuid.uuid4().hex}"
    configure_mock_llm(mock_url, [{"text": answer, "block": True}], key=model)
    events = EventLog(tmp_path / "network.jsonl")
    with ExitStack() as resources:
        a = resources.enter_context(
            server_runner(
                tmp_path / "a",
                server_cwd=_REPO,
                server_bootstrap=_SERVER_BOOTSTRAP,
                base_env=base_env,
                server_env=env,
            )
        )
        b = resources.enter_context(
            server_runner(
                tmp_path / "b",
                server_cwd=_REPO,
                server_bootstrap=_SERVER_BOOTSTRAP,
                base_env=base_env,
                server_env=env,
                database_uri=a.database_uri,
                artifact_location=a.artifact_location,
                binding_token=a._token,
            )
        )
        loop = LoopThread("retirement-network")
        loop.start()
        resources.callback(loop.stop)
        proxy = FaultProxy(
            "runner", ("127.0.0.1", a.port), loop=loop, events=events, classify=host_link
        )
        proxy.start()
        resources.callback(proxy.stop)
        a.start_runner(cwd=_REPO, env={**env, "RUNNER_SERVER_URL": proxy.url})
        headers = {
            "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
            "x-omnigent-background-session-titles": "off",
        }
        ca = resources.enter_context(
            httpx.Client(base_url=a.base_url, headers=headers, timeout=30, trust_env=False)
        )
        cb = resources.enter_context(
            httpx.Client(base_url=b.base_url, headers=headers, timeout=30, trust_env=False)
        )
        name = register_inline_agent(
            ca,
            name=model,
            harness="openai-agents",
            model=model,
            profile="",
            prompt="Return the configured answer.",
            mock_llm_base_url=f"{mock_url}/v1",
        )
        session_id = create_runner_bound_session(ca, agent_name=name, runner_id=a.runner_id)
        ownership = {"a": _state(ca, a.runner_id), "b": _state(cb, a.runner_id)}
        assert ownership["a"]["source_file"] == str(_REPO / "omnigent" / "__init__.py")
        assert ownership["b"]["source_file"] == ownership["a"]["source_file"]
        (tmp_path / "ownership.json").write_text(json.dumps(ownership, indent=2))
        _send_user_message(ca, session_id)
        _poll_until(
            lambda: _gate_pending(mock_url),
            timeout=45,
            what="real model turn blocked on its reply",
        )
        assert _session_snapshot(ca, session_id)["status"] == "running"
        refusal = proxy.refuse({"runner.tunnel"})
        resources.callback(refusal.clear)
        proxy.upstream = ("127.0.0.1", b.port)
        started = time.monotonic()
        if outcome == "ordinary_dead":
            assert a.runner is not None
            a.runner.kill()
        else:
            response = ca.post(f"/__runner_lifecycle/retire/{a.runner_id}")
            response.raise_for_status()
            assert response.json()["retired"] is True
            if outcome == "retired_dead":
                assert a.runner is not None
                a.runner.kill()
        events.emit("test", "fault_started", outcome=outcome, session_id=session_id)
        observations = []
        if outcome == "handoff":
            while time.monotonic() - started < 100:
                snapshot = _session_snapshot(ca, session_id)
                observations.append(
                    {
                        "elapsed": time.monotonic() - started,
                        "status": snapshot["status"],
                        "error": snapshot.get("last_task_error"),
                    }
                )
                (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))
                assert snapshot["status"] != "failed", snapshot
                time.sleep(2)
            refusal.clear()
            _poll_until(
                lambda: _state(cb, a.runner_id)["runner_local"],
                timeout=35,
                what="same runner accepted by replica B",
            )
            events.emit("test", "reconnected", elapsed=time.monotonic() - started)
            release_mock_gate(mock_url)
            _poll_until(
                lambda: answer in _session_blob(cb, session_id),
                timeout=30,
                what="original turn completion",
            )
            while time.monotonic() - started < 183:
                assert _session_snapshot(cb, session_id)["status"] != "failed"
                time.sleep(2)
            offline_skip = (
                f"Runner {a.runner_id} is live on another replica; skipping offline-marking"
            )
            _poll_until(
                lambda: (
                    f"session={session_id} (live_elsewhere)" in _server_log(a)
                    and offline_skip in _server_log(a)
                ),
                timeout=15,
                what="both old-replica disconnect paths to relinquish the recovered runner",
            )
            configure_mock_llm(mock_url, [{"text": followup}], key=model)
            _send_user_message(cb, session_id, "Send the follow-up answer.")
            _poll_until(
                lambda: followup in _session_blob(cb, session_id),
                timeout=45,
                what="follow-up turn completion",
            )
            _poll_until(
                lambda: _session_snapshot(cb, session_id)["status"] == "idle",
                timeout=15,
                what="follow-up idle status",
            )
            snapshot = _session_snapshot(cb, session_id)
            assert snapshot["status"] == "idle"
            assert snapshot.get("last_task_error") is None
            assert f"session turn failed for {session_id}" not in _server_log(a)
        else:
            expected = 90 if outcome == "ordinary_dead" else 180
            while time.monotonic() - started < expected + 25:
                snapshot = _session_snapshot(ca, session_id)
                elapsed = time.monotonic() - started
                observations.append(
                    {
                        "elapsed": elapsed,
                        "status": snapshot["status"],
                        "error": snapshot.get("last_task_error"),
                    }
                )
                (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))
                # The failed status is published just before its cause labels persist.
                if snapshot["status"] == "failed" and snapshot.get("last_task_error"):
                    break
                time.sleep(2)
            assert snapshot["status"] == "failed", snapshot
            assert expected - 3 <= elapsed <= expected + 25, observations
            assert (snapshot.get("last_task_error") or {}).get("code") == "runner_disconnected", (
                snapshot
            )
            release_mock_gate(mock_url)
        (tmp_path / "final-snapshot.json").write_text(json.dumps(snapshot, indent=2))
