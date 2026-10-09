"""Failure paths in the disposable Kubernetes example must clean up and explain why."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "deploy/kubernetes/prototype"


@pytest.mark.parametrize("failed_command", ["logs", "cp", "write", "missing-binary"])
async def test_evidence_failure_still_removes_host_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_command: str
) -> None:
    spec = importlib.util.spec_from_file_location("nginx_verify", PROTOTYPE / "verify.py")
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    process = Mock()
    monkeypatch.setattr(verifier.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        verifier, "eventually", AsyncMock(side_effect=RuntimeError("host setup failed"))
    )

    async def command(*args: str) -> str:
        if failed_command == "missing-binary" and args[:2] == ("docker", "logs"):
            raise FileNotFoundError("docker is unavailable")
        if args[:2] == ("docker", failed_command):
            raise RuntimeError("evidence is unavailable")
        return ""

    original_write = Path.write_text

    def write(path: Path, *args, **kwargs):
        if failed_command == "write" and path.name == "sse.log":
            raise OSError("disk is full")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    commands = AsyncMock(side_effect=command)
    monkeypatch.setattr(verifier, "command", commands)
    args = argparse.Namespace(
        url="http://localhost:18081",
        kubeconfig=tmp_path / "kubeconfig",
        output=tmp_path / "evidence",
        host_id="cleanup-test",
        replicas=2,
        mock_port=0,
    )
    with pytest.raises(RuntimeError, match="host setup failed"):
        await verifier.verify(args)

    commands.assert_any_await("docker", "rm", "-f", "omnigent-prototype-client-cleanup-")
    process.terminate.assert_called_once()
    assert "host setup failed" in json.loads((args.output / "report.json").read_text())["error"]
    assert args.mock_port > 0, "the rollout coordinator cannot probe the allocated mock port"


async def test_transient_pod_query_failure_is_retried() -> None:
    spec = importlib.util.spec_from_file_location("nginx_verify", PROTOTYPE / "verify.py")
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    query = AsyncMock(side_effect=[RuntimeError("Kubernetes API unavailable"), True])
    assert await verifier.eventually(query, timeout=2)
    assert query.await_count == 2


@pytest.mark.parametrize("failure", [OSError, RuntimeError, asyncio.CancelledError])
async def test_log_follower_closes_its_file_if_startup_fails(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    spec = importlib.util.spec_from_file_location("nginx_verify", PROTOTYPE / "verify.py")
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    handle = io.StringIO()
    path = Mock()
    path.open.return_value = handle
    monkeypatch.setattr(
        verifier,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=AsyncMock(side_effect=failure()),
            subprocess=asyncio.subprocess,
            CancelledError=asyncio.CancelledError,
        ),
    )
    with pytest.raises(failure):
        await verifier.start_log_follower(["kubectl"], "pod", path)
    assert handle.closed


@pytest.mark.skipif(os.name != "posix", reason="the prototype uses Linux host networking")
async def test_mock_keeps_its_ephemeral_port_until_the_child_is_ready(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("nginx_verify", PROTOTYPE / "verify.py")
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


@pytest.mark.parametrize("failed_capture", ["observer", "response"])
async def test_browser_evidence_failure_cannot_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_capture: str
) -> None:
    monkeypatch.syspath_prepend(str(PROTOTYPE))
    spec = importlib.util.spec_from_file_location("nginx_browser", PROTOTYPE / "verify_browser.py")
    assert spec is not None and spec.loader is not None
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    monkeypatch.setattr(recorder, "command", AsyncMock(return_value=""))
    args = argparse.Namespace(url="http://localhost:18081", output=tmp_path, capture_streams=False)
    host = recorder.BrowserHost(args, 1, "test-host", "upstream", "pod", 0, time.monotonic())
    host.report["turns"] = [
        {
            "reply_visible": True,
            "saved_user_count": 1,
            "saved_assistant_count": 1,
            "rendered_user_count": 1,
            "rendered_assistant_count": 1,
            "model_request_count": 1,
        }
    ]
    host.report["final_session_status"] = "idle"
    if failed_capture == "observer":
        host.page = AsyncMock()
        host.page.evaluate.return_value = None
        host.monitor_task = asyncio.create_task(host.observe_ui())
        with pytest.raises(RuntimeError, match="observer disappeared"):
            await host.monitor_task
    else:

        async def failed_response():
            raise OSError("response evidence unavailable")

        host.response_tasks.append(asyncio.create_task(failed_response()))
    await host.cleanup()
    assert not host.report["passed"]
    assert host.report["cleanup_errors"]


def _run_up(
    tmp_path: Path,
    *,
    port: str,
    migration: str,
    cluster_exists: bool = True,
    transient: bool = False,
) -> tuple[subprocess.CompletedProcess, str]:
    state = tmp_path / "state"
    state.mkdir()
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
        "PROTOTYPE_PORT": "18081",
        "KIND_BIN": str(bin_dir / "kind"),
        "PROTOTYPE_TEST_COMMANDS": str(commands),
        "PROTOTYPE_TEST_PORT": port,
        "PROTOTYPE_TEST_MIGRATION": migration,
        "PROTOTYPE_TEST_CLUSTER_EXISTS": str(cluster_exists).lower(),
        "PROTOTYPE_TEST_TRANSIENT": str(transient).lower(),
    }
    result = subprocess.run(
        ["bash", str(PROTOTYPE / "run.sh"), "up"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, commands.read_text()


def test_existing_cluster_rejects_changed_port_before_building(tmp_path: Path) -> None:
    result, commands = _run_up(tmp_path, port="127.0.0.1:19000", migration="Complete=True")
    assert result.returncode == 1
    assert "down before changing the port" in result.stderr
    assert "Ready:" not in result.stdout
    assert "docker build" not in commands


def test_deleted_cluster_reports_how_to_reset_stale_kubeconfig(tmp_path: Path) -> None:
    result, commands = _run_up(tmp_path, port="", migration="Complete=True", cluster_exists=False)
    assert result.returncode == 1
    assert "no running cluster container" in result.stderr
    assert "down and retry" in result.stderr
    assert "docker build" not in commands


def test_failed_migration_reports_logs_without_waiting_for_timeout(tmp_path: Path) -> None:
    result, commands = _run_up(tmp_path, port="127.0.0.1:18081", migration="Failed=True")
    assert result.returncode == 1
    assert "Database migration failed" in result.stderr
    assert "migration diagnostic" in result.stderr
    assert "sleep " not in commands
    assert "server.yaml" not in commands


def test_completed_migration_allows_deployment(tmp_path: Path) -> None:
    result, commands = _run_up(tmp_path, port="127.0.0.1:18081", migration="Complete=True")
    assert result.returncode == 0, result.stderr
    assert "Ready: http://localhost:18081" in result.stdout
    assert "server.yaml" in commands


def test_transient_migration_query_failure_is_retried(tmp_path: Path) -> None:
    result, commands = _run_up(
        tmp_path, port="127.0.0.1:18081", migration="Complete=True", transient=True
    )
    assert result.returncode == 0, result.stderr
    assert commands.count("get job migrate") == 2
    assert "server.yaml" in commands
