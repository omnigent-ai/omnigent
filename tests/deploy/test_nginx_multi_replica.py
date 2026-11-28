"""Failure paths in the disposable Kubernetes example must clean up and explain why."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import socket
import subprocess
from itertools import cycle
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "deploy/kubernetes/multi_replica"


@pytest.fixture
def verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Complete the rollout with fake transports so cleanup failures are deterministic."""
    spec = importlib.util.spec_from_file_location("nginx_verify", EXAMPLE / "verify.py")
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    monkeypatch.setattr(verifier.uuid, "uuid4", lambda: SimpleNamespace(hex="cleanup-test"))
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(verifier, "start_mock_server", lambda *_: (process, 18082))
    backends = cycle(("server-a", "server-b"))

    def response(*_args, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://localhost"),
            headers={"x-omnigent-upstream": next(backends)},
            json={"id": "session", "data": [{"agent_id": "agent"}], "released": 1},
        )

    async def lines():
        yield "data: connected"
        await asyncio.Event().wait()

    stream = Mock()
    stream.aiter_lines = lines
    client = AsyncMock()
    client.get.side_effect = response
    client.post.side_effect = response
    client.stream = Mock(return_value=AsyncMock())
    client.stream.return_value.__aenter__.return_value = stream
    monkeypatch.setattr(verifier.httpx, "AsyncClient", lambda **_: client)
    ws = AsyncMock(close_code=1006)
    ws.response = SimpleNamespace(headers={"x-omnigent-upstream": "server-c"})
    monkeypatch.setattr(verifier, "connect", AsyncMock(return_value=ws))
    monkeypatch.setattr(
        verifier, "terminal_reply", AsyncMock(return_value=SimpleNamespace(group=lambda _: "39"))
    )

    async def ready(check, **_kwargs):
        if check.__name__ in {"mock_ready", "ingress_ready", "reattach_terminal"}:
            return await check()
        return True

    monkeypatch.setattr(verifier, "eventually", ready)
    pods = json.dumps({"items": [{"metadata": {"name": name}} for name in ("pod-a", "pod-b")]})
    commands = AsyncMock(return_value=pods)
    monkeypatch.setattr(verifier, "command", commands)
    args = argparse.Namespace(
        url="http://localhost:18081",
        kubeconfig=tmp_path / "kubeconfig",
        output=tmp_path / "evidence",
        mock_port=0,
    )
    return SimpleNamespace(verifier=verifier, args=args, commands=commands, process=process)


@pytest.mark.parametrize("failed_command", ["logs", "cp", "write", "missing-binary", "mock-wait"])
async def test_evidence_failure_still_removes_host_container(
    verification, monkeypatch: pytest.MonkeyPatch, failed_command: str
) -> None:
    verifier, args, commands, process = (
        verification.verifier,
        verification.args,
        verification.commands,
        verification.process,
    )
    if failed_command == "mock-wait":
        process.wait.side_effect = subprocess.TimeoutExpired("mock", 5)

    async def command(*args: str) -> str:
        if args[:2] == ("docker", "exec"):
            raise RuntimeError("host setup failed")
        if failed_command == "missing-binary" and args[:2] == ("docker", "logs"):
            raise FileNotFoundError("docker is unavailable")
        if args[:2] == ("docker", failed_command):
            raise RuntimeError("evidence is unavailable")
        return commands.return_value

    original_write = Path.write_text

    def write(path: Path, *args, **kwargs):
        if failed_command == "write" and path.name == "sse.log":
            raise OSError("disk is full")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    commands.side_effect = command
    with pytest.raises(RuntimeError, match="host setup failed"):
        await verifier.verify(args)

    commands.assert_any_await("docker", "rm", "-f", "omnigent-prototype-client-cleanup-")
    process.terminate.assert_called_once()
    report = json.loads((args.output / "report.json").read_text())
    assert "host setup failed" in report["error"]
    assert report["mock_port"] > 0
    assert args.mock_port == 0


@pytest.mark.parametrize("failure", ["container-removal", "mock-wait"])
async def test_cleanup_failure_fails_successful_verification(verification, capsys, failure):
    async def command(*args: str) -> str:
        if failure == "container-removal" and args[:2] == ("docker", "rm"):
            raise RuntimeError("Docker daemon unavailable")
        return verification.commands.return_value

    verification.commands.side_effect = command
    if failure == "mock-wait":
        verification.process.wait.side_effect = subprocess.TimeoutExpired("mock", 5)

    with pytest.raises(RuntimeError, match="Required cleanup failed"):
        await verification.verifier.verify(verification.args)

    report = json.loads((verification.args.output / "report.json").read_text())
    assert report["followup_turn_completed"] is True
    assert report["passed"] is False
    assert report["cleanup_error"]
    assert "PASS:" not in capsys.readouterr().out
    verification.commands.assert_any_await(
        "docker", "rm", "-f", "omnigent-prototype-client-cleanup-"
    )


async def test_transient_pod_query_failure_is_retried() -> None:
    spec = importlib.util.spec_from_file_location("nginx_verify", EXAMPLE / "verify.py")
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    query = AsyncMock(side_effect=[RuntimeError("Kubernetes API unavailable"), True])
    assert await verifier.eventually(query, timeout=2)
    assert query.await_count == 2


@pytest.mark.skipif(os.name != "posix", reason="the example uses Linux host networking")
async def test_mock_keeps_its_ephemeral_port_until_the_child_is_ready(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("nginx_verify", EXAMPLE / "verify.py")
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    with (tmp_path / "mock.log").open("w") as log:
        process, port = verifier.start_mock_server(0, log)
        try:
            with socket.socket() as competing_listener:
                with pytest.raises(OSError):
                    competing_listener.bind(("127.0.0.1", port))
            async with httpx.AsyncClient(trust_env=False) as client:

                async def ready():
                    response = await client.get(f"http://127.0.0.1:{port}/stats")
                    return response.json() == {"request_count": 0}

                assert await verifier.eventually(ready, timeout=15)
        finally:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)


def _run_script(
    tmp_path: Path,
    *,
    port: str,
    migration: str,
    cluster_exists: bool = True,
    transient: bool = False,
    kubeconfig_exists: bool = True,
    configured_port: str = "18081",
    action: str = "up",
) -> tuple[subprocess.CompletedProcess, str]:
    state = tmp_path / "state"
    state.mkdir()
    if kubeconfig_exists:
        (state / "kubeconfig").touch()
    commands = tmp_path / "commands"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "shim"
    shim.write_text(
        """#!/usr/bin/env bash
name=${0##*/}
printf '%s %s\\n' "$name" "$*" >> "$PROTOTYPE_TEST_COMMANDS"
case "$name $*" in
  'docker port '*)
    [[ "$PROTOTYPE_TEST_CLUSTER_EXISTS" == true ]] || exit 1
    printf '%s\\n' "$PROTOTYPE_TEST_PORT" ;;
  'kubectl '*"get job migrate"*)
    if [[ "$PROTOTYPE_TEST_TRANSIENT" == true && ! -f "$PROTOTYPE_TEST_COMMANDS.retry" ]]; then
      touch "$PROTOTYPE_TEST_COMMANDS.retry"
      printf 'API temporarily unavailable\\n' >&2
      exit 1
    fi
    printf '%s\\n' "$PROTOTYPE_TEST_MIGRATION" ;;
  'kubectl '*"logs job/migrate"*) printf 'migration diagnostic\\n' ;;
esac
"""
    )
    shim.chmod(0o755)
    for name in ("docker", "kubectl", "kind", "sleep"):
        (bin_dir / name).symlink_to(shim)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PROTOTYPE_STATE_DIR": str(state),
        "PROTOTYPE_PORT": configured_port,
        "KIND_BIN": str(bin_dir / "kind"),
        "PROTOTYPE_TEST_COMMANDS": str(commands),
        "PROTOTYPE_TEST_PORT": port,
        "PROTOTYPE_TEST_MIGRATION": migration,
        "PROTOTYPE_TEST_CLUSTER_EXISTS": str(cluster_exists).lower(),
        "PROTOTYPE_TEST_TRANSIENT": str(transient).lower(),
    }
    result = subprocess.run(
        ["bash", str(EXAMPLE / "run.sh"), action],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, commands.read_text() if commands.exists() else ""


def test_existing_cluster_rejects_changed_port_before_building(tmp_path: Path) -> None:
    result, commands = _run_script(tmp_path, port="127.0.0.1:19000", migration="Complete=True")
    assert result.returncode == 1
    assert "down before changing the port" in result.stderr
    assert "Ready:" not in result.stdout
    assert "docker build" not in commands


def test_deleted_cluster_reports_how_to_reset_stale_kubeconfig(tmp_path: Path) -> None:
    result, commands = _run_script(
        tmp_path, port="", migration="Complete=True", cluster_exists=False
    )
    assert result.returncode == 1
    assert "no running cluster container" in result.stderr
    assert "down and retry" in result.stderr
    assert "docker build" not in commands


def test_failed_migration_reports_logs_without_waiting_for_timeout(tmp_path: Path) -> None:
    result, commands = _run_script(tmp_path, port="127.0.0.1:18081", migration="Failed=True")
    assert result.returncode == 1
    assert "Database migration failed" in result.stderr
    assert "migration diagnostic" in result.stderr
    assert "sleep " not in commands
    assert "server.yaml" not in commands


@pytest.mark.parametrize("port", ["127.0.0.1:18081", "127.0.0.1:18081\n[::1]:18081"])
def test_completed_migration_allows_deployment(tmp_path: Path, port: str) -> None:
    result, commands = _run_script(tmp_path, port=port, migration="Complete=True")
    assert result.returncode == 0, result.stderr
    assert "Ready: http://localhost:18081" in result.stdout
    assert "server.yaml" in commands


def test_transient_migration_query_failure_is_retried(tmp_path: Path) -> None:
    result, commands = _run_script(
        tmp_path, port="127.0.0.1:18081", migration="Complete=True", transient=True
    )
    assert result.returncode == 0, result.stderr
    assert commands.count("get job migrate") == 2
    assert "server.yaml" in commands


def test_fresh_cluster_uses_the_requested_local_port(tmp_path: Path) -> None:
    result, commands = _run_script(
        tmp_path,
        port="",
        migration="Complete=True",
        kubeconfig_exists=False,
        configured_port="19000",
    )
    assert result.returncode == 0, result.stderr
    assert "kind create cluster --name omnigent-nginx-prototype" in commands
    assert f"--config {tmp_path / 'state/kind.yaml'}" in commands
    assert "docker port " not in commands

    config = yaml.safe_load((tmp_path / "state/kind.yaml").read_text())
    assert config["nodes"][0]["extraPortMappings"] == [
        {
            "containerPort": 30080,
            "hostPort": 19000,
            "listenAddress": "127.0.0.1",
            "protocol": "TCP",
        }
    ]
    assert "Ready: http://localhost:19000" in result.stdout


def test_verify_requires_cluster_state_before_building(tmp_path: Path) -> None:
    result, commands = _run_script(
        tmp_path, port="", migration="", kubeconfig_exists=False, action="verify"
    )
    assert result.returncode == 1
    assert "up first" in result.stderr
    assert commands == "", "verification ran a command before checking its kubeconfig"
