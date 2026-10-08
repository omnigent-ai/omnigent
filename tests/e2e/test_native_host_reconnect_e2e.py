"""A single native send survives the real host daemon dying and restarting.

Owns a real server, host daemon, host-launched runners, and Codex CLI. Requires
live model credentials; no model, tunnel, database binding, or timer is mocked.
The first turn proves actual tool execution before the host is SIGKILLed. A
second send stays in flight while the host is absent beyond the old-runner
connect grace. Restarting the same host must execute that input without resend.

Run with OMNIGENT_E2E_CODEX_NATIVE=1 and the E2E suite's --llm-api-key and
optional --profile arguments. OMNIGENT_E2E_CODEX_MODEL overrides the live model.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import TypeVar

import httpx
import pytest

from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from tests._helpers.live_server import local_server_env, terminate_process
from tests._helpers.server_runner import server_runner

_REPO_ROOT = Path(__file__).resolve().parents[2]
_T = TypeVar("_T")

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1"
        or any(shutil.which(binary) is None for binary in ("codex", "tmux")),
        reason="requires Codex, tmux, and OMNIGENT_E2E_CODEX_NATIVE=1",
    ),
]


def _wait(check: Callable[[], _T], description: str, timeout: float = 60) -> _T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _items(client: httpx.Client, session_id: str) -> list[dict]:
    return _get(client, f"/v1/sessions/{session_id}/items?limit=100&order=asc")["data"]


def _message(text: str) -> dict:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def _text(item: dict) -> str:
    return "".join(block.get("text", "") for block in item.get("content", []))


def _host_online(client: httpx.Client, host_id: str) -> bool:
    return any(
        row["host_id"] == host_id and row["status"] == "online"
        for row in _get(client, "/v1/hosts")["hosts"]
    )


def _wait_for_work(
    client: httpx.Client,
    session_id: str,
    proof: Path,
    expected: str,
    reply: str,
) -> list[dict]:
    def finished() -> list[dict] | None:
        rows = _items(client, session_id)
        errors = [row for row in rows if row.get("type") == "error"]
        assert not errors, f"Original input failed instead of reaching Codex: {errors}"
        if (
            proof.exists()
            and proof.read_text() == expected
            and any(row.get("role") == "assistant" and reply in _text(row) for row in rows)
            and _get(client, f"/v1/sessions/{session_id}")["status"] == "idle"
        ):
            return rows
        return None

    return _wait(finished, f"Codex to execute tools and finish {reply}", timeout=180)


def test_native_message_survives_host_restart(
    tmp_path: Path,
    llm_api_key: str,
    using_mock_llm: bool,
    databricks_workspace_host: str | None,
) -> None:
    """One HTTP send must resume real tool work after a parent-death outage."""
    if using_mock_llm:
        pytest.skip("requires live --llm-api-key; a mock model is not this regression's proof")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nonce = uuid.uuid4().hex + "\n"
    (workspace / "challenge.txt").write_text(nonce)
    config_root = tmp_path / "config"
    config_root.mkdir()
    model = os.environ.get("OMNIGENT_E2E_CODEX_MODEL") or (
        "databricks-gpt-5-4-mini" if databricks_workspace_host else "gpt-5.4-mini"
    )
    base_url = (
        f"{databricks_workspace_host}/serving-endpoints"
        if databricks_workspace_host
        else "https://api.openai.com/v1"
    )
    (config_root / "config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "e2e-live": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": base_url,
                            "api_key_ref": "env:OPENAI_API_KEY",
                            "wire_api": "responses",
                            "models": {"default": model},
                        },
                    }
                },
            }
        )
    )
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    (tmp_path / "codex-config").mkdir()
    native_env = {
        "OMNIGENT_CONFIG_HOME": str(config_root),
        "CODEX_HOME": str(tmp_path / "codex-config"),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(tmp_path / "codex-state"),
        "OPENAI_BASE_URL": base_url,
        "OPENAI_API_KEY": llm_api_key,
    }
    evidence: dict = {
        "model": model,
        "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "recovery_send_count": 0,
    }
    started = time.monotonic()

    def record(event: str, **details: object) -> None:
        evidence[event] = {"elapsed_s": round(time.monotonic() - started, 3), **details}
        (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"{event}: {json.dumps(evidence[event])}", flush=True)

    with ExitStack() as resources:
        stack = resources.enter_context(
            server_runner(
                tmp_path,
                base_env=base_env,
                server_cwd=_REPO_ROOT,
                server_env={**native_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": None},
                workspace=workspace,
                poll_interval=0.1,
            )
        )
        client = resources.enter_context(
            httpx.Client(
                base_url=stack.base_url,
                trust_env=False,
                timeout=10,
                headers={"x-omnigent-background-session-titles": "off"},
            )
        )

        def spawn_host(generation: int) -> subprocess.Popen[bytes]:
            log = resources.enter_context((tmp_path / f"host-{generation}.log").open("wb"))
            process = subprocess.Popen(
                [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", stack.base_url],
                cwd=_REPO_ROOT,
                env=local_server_env(
                    {
                        **native_env,
                        "HOME": str(stack.runner_home),
                        "OMNIGENT_DATA_DIR": str(stack.runner_home / ".omnigent"),
                        "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
                    },
                    base_env=base_env,
                ),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            resources.callback(terminate_process, process)
            return process

        host = spawn_host(1)
        online_hosts = _wait(
            lambda: [
                row for row in _get(client, "/v1/hosts")["hosts"] if row["status"] == "online"
            ],
            "the real host daemon to register",
        )
        assert len(online_hosts) == 1, online_hosts
        host_id = online_hosts[0]["host_id"]
        agent = next(
            row
            for row in _get(client, "/v1/agents")["data"]
            if row["name"] == CODEX_NATIVE_AGENT_NAME
        )
        response = client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "host_id": host_id, "workspace": str(workspace)},
            timeout=90,
        )
        response.raise_for_status()
        session_id = response.json()["id"]
        initial_prompt = (
            "Use your shell tool to run `cp challenge.txt initial-proof.txt` in the current "
            "workspace, then reply exactly INITIAL_DONE. Do not merely describe the command."
        )
        response = client.post(
            f"/v1/sessions/{session_id}/events", json=_message(initial_prompt), timeout=90
        )
        response.raise_for_status()
        _wait_for_work(client, session_id, workspace / "initial-proof.txt", nonce, "INITIAL_DONE")
        old_runner_id = _get(client, f"/v1/sessions/{session_id}")["runner_id"]
        assert old_runner_id is not None
        record(
            "initial_turn_completed",
            session_id=session_id,
            host_id=host_id,
            runner_id=old_runner_id,
        )

        # SIGKILL exercises the runner's real parent-death watcher, not Stop.
        host.kill()
        host.wait(timeout=10)
        _wait(lambda: not _host_online(client, host_id), "the host tunnel to disconnect")
        _wait(
            lambda: not _get(client, f"/v1/runners/{old_runner_id}/status")["online"],
            "the orphaned runner to disconnect after its host dies",
        )
        record("host_and_runner_offline", host_returncode=host.returncode)

        recovery_prompt = (
            "Use your shell tool to run `cat challenge.txt >> recovery-proof.txt` exactly once "
            "in the current workspace. Do not truncate the output file. Then reply exactly "
            "RECOVERY_DONE. Do not merely describe the command."
        )

        def send_recovery() -> tuple[httpx.Response, float]:
            result = client.post(
                f"/v1/sessions/{session_id}/events",
                json=_message(recovery_prompt),
                timeout=150,
            )
            return result, time.monotonic() - started

        log_offset = stack.log_path("server").stat().st_size
        with ThreadPoolExecutor(max_workers=1) as executor:
            evidence["recovery_send_count"] += 1
            record("original_send_started")
            pending = executor.submit(send_recovery)
            _wait(
                lambda: (
                    f"for host-bound runner {old_runner_id} to register".encode()
                    in stack.log_path("server").read_bytes()[log_offset:]
                ),
                "the real request to enter its old-runner grace",
                timeout=15,
            )
            record("old_runner_grace_started")
            # Keep the host absent past the unmodified 10s runner grace.
            time.sleep(25)
            assert not _host_online(client, host_id)
            assert not (workspace / "recovery-proof.txt").exists()
            record("host_restart_started", request_already_returned=pending.done())
            replacement_host = spawn_host(2)
            _wait(lambda: _host_online(client, host_id), "the same host identity to return")
            assert replacement_host.poll() is None
            record("same_host_reconnected")
            response, response_elapsed_s = pending.result(timeout=150)
            response.raise_for_status()
            rows = _items(client, session_id)
            record(
                "original_send_returned",
                response_elapsed_s=round(response_elapsed_s, 3),
                http_status=response.status_code,
                response=response.json(),
                errors=[row for row in rows if row.get("type") == "error"],
            )

        rows = _wait_for_work(
            client, session_id, workspace / "recovery-proof.txt", nonce, "RECOVERY_DONE"
        )
        new_runner_id = _get(client, f"/v1/sessions/{session_id}")["runner_id"]
        assert new_runner_id and new_runner_id != old_runner_id
        assert _get(client, f"/v1/runners/{new_runner_id}/status")["online"] is True
        assert (
            sum(row.get("role") == "user" and _text(row) == recovery_prompt for row in rows) == 1
        )
        assert (
            sum(row.get("role") == "assistant" and "RECOVERY_DONE" in _text(row) for row in rows)
            == 1
        )
        assert evidence["recovery_send_count"] == 1
        assert (workspace / "recovery-proof.txt").read_text() == nonce
        record("recovered_work_completed_once", runner_id=new_runner_id, proof_lines=1)
