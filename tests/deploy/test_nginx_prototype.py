"""Failure paths in the disposable Kubernetes example must clean up and explain why."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "deploy/kubernetes/prototype"


@pytest.mark.parametrize("failed_command", ["logs", "cp"])
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
        if args[:2] == ("docker", failed_command):
            raise RuntimeError("evidence is unavailable")
        return ""

    commands = AsyncMock(side_effect=command)
    monkeypatch.setattr(verifier, "command", commands)
    args = argparse.Namespace(
        url="http://localhost:18081",
        kubeconfig=tmp_path / "kubeconfig",
        output=tmp_path / "evidence",
        host_id="cleanup-test",
        replicas=2,
        mock_port=18082,
    )
    with pytest.raises(RuntimeError, match="host setup failed"):
        await verifier.verify(args)

    commands.assert_any_await("docker", "rm", "-f", "omnigent-prototype-client-cleanup-")
    process.terminate.assert_called_once()
    assert "host setup failed" in json.loads((args.output / "report.json").read_text())["error"]


def _run_up(
    tmp_path: Path, *, port: str, migration: str, cluster_exists: bool = True
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
  'kubectl '*"get job migrate"*) printf '%s\\n' "$PROTOTYPE_TEST_MIGRATION" ;;
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
