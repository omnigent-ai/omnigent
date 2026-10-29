"""A cold bundle load must leave the real runner tunnel serving heartbeats."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    poll_session_until_terminal,
    send_user_message_to_session,
)

_REPO = Path(__file__).resolve().parents[2]
_SERVER_BOOTSTRAP = """
import os
import sys
from omnigent.server.routes import runner_tunnel
runner_tunnel.PING_INTERVAL_S = 0.1
runner_tunnel.PING_MISS_THRESHOLD = 50
sys.argv.extend(["--config", os.environ["E2E_SERVER_CONFIG"]])
from omnigent.cli import main
main()
"""
_RUNNER_BOOTSTRAP = """
import json
import os
import threading
import time
from pathlib import Path
import omnigent.spec

original_load = omnigent.spec.load
gate = Path(os.environ["E2E_BUNDLE_GATE"])

def load_with_gate(source, *args, **kwargs):
    if isinstance(source, bytes) and not gate.with_suffix(".entered").exists():
        gate.with_suffix(".entered").write_text(json.dumps({
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "source_file": omnigent.spec.__file__,
        }))
        deadline = time.monotonic() + 30
        while not gate.with_suffix(".release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test did not release the bundle load gate")
            time.sleep(0.01)
    return original_load(source, *args, **kwargs)

omnigent.spec.load = load_with_gate
from omnigent.runner._entry import main
main()
"""


@pytest.mark.timeout(180)
def test_cold_bundle_load_keeps_tunnel_heartbeats_and_completes_turn(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """Hold parsing in the runner, observe real pongs, then finish a model turn."""
    gate = tmp_path / "bundle-gate"
    entered = gate.with_suffix(".entered")
    release = gate.with_suffix(".release")
    config = tmp_path / "server-config.yaml"
    config.write_text(
        json.dumps({"debug_router_modules": ["tests._helpers.runner_lifecycle_controls"]})
    )
    model = "bundle-heartbeat-model"
    answer = "BUNDLE_HEARTBEAT_REPLY"
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": answer}], key=model)
    bundle = bundle_files(
        {
            "heartbeat.yaml": json.dumps(
                {
                    "name": "bundle-heartbeat",
                    "prompt": "Return the configured answer.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{isolated_mock_llm_server_url}/v1",
                        },
                    },
                }
            ).encode()
        }
    )
    observations = []
    with server_runner(
        tmp_path / "stack",
        server_cwd=_REPO,
        server_bootstrap=_SERVER_BOOTSTRAP,
        server_env={"E2E_SERVER_CONFIG": str(config)},
        base_env={key: value for key, value in os.environ.items() if key in {"PATH", "LANG"}},
        poll_interval=0.1,
    ) as stack:
        stack.start_runner(
            cwd=_REPO,
            bootstrap=_RUNNER_BOOTSTRAP,
            env={"E2E_BUNDLE_GATE": str(gate), "OMNIGENT_RUNNER_ZYGOTE": "0"},
        )
        with httpx.Client(
            base_url=stack.base_url,
            headers={
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                "x-omnigent-background-session-titles": "off",
            },
            trust_env=False,
            timeout=30,
        ) as client:

            def tunnel_state() -> dict:
                response = client.get(f"/__runner_lifecycle/state/{stack.runner_id}")
                response.raise_for_status()
                state = response.json()
                observations.append(state)
                return state

            created = post_session_bundle(client.post, "/v1/sessions", bundle)
            created.raise_for_status()
            session_id = created.json()["session_id"]
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending_bind = pool.submit(
                    bind_session_runner,
                    client.patch,
                    stack.base_url,
                    session_id,
                    stack.runner_id,
                    timeout=30,
                )
                try:
                    poll_until(entered.exists, timeout_s=30, interval_s=0.05)
                    initial = tunnel_state()
                    assert initial["runner_local"], initial
                    observed_at = initial["monotonic_time"]
                    # Require successive actual pongs after parsing entered the gate.
                    for _ in range(5):

                        def fresh_pong(after: float = observed_at) -> bool:
                            state = tunnel_state()
                            assert state["connection_id"] == initial["connection_id"], (
                                "runner tunnel disconnected while bundle parsing was held",
                                state,
                            )
                            diagnostics = state["tunnel_diagnostics"]
                            pong_age = diagnostics["last_app_pong_received_age_s"]
                            return (
                                pong_age is not None
                                and pong_age < 0.15
                                and state["monotonic_time"] - pong_age > after + 0.15
                            )

                        poll_until(fresh_pong, timeout_s=3, interval_s=0.05)
                        observed_at = observations[-1]["monotonic_time"]
                        assert not release.exists()
                finally:
                    release.touch()
                    (tmp_path / "heartbeat-observations.json").write_text(
                        json.dumps(observations, indent=2)
                    )
                pending_bind.result(timeout=30)
            response_id = send_user_message_to_session(
                client, session_id=session_id, content="Please reply after loading the bundle."
            )
            result = poll_session_until_terminal(
                client, session_id=session_id, response_id=response_id, timeout=60
            )
            (tmp_path / "completed-turn.json").write_text(json.dumps(result, indent=2))
            assert result["status"] == "completed", result
            assert answer in json.dumps(result["output"]), result
            assert stack.runner is not None and stack.runner.poll() is None
