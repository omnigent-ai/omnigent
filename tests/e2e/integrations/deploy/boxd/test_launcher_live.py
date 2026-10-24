"""Optional live test of every boxd launcher primitive against real machines.

Drives the real :class:`~omnigent.onboarding.sandboxes.boxd.BoxdSandboxLauncher`
through what the managed-host and CLI-bootstrap flows rely on: provision with
the host bootstrap, run (exit codes, stderr, env passthrough), put, streaming,
the foreground attach, keep-alive, resume from standby, hibernation and a stop,
status, and an idempotent terminate. Every machine it creates is deleted, also
when a check fails.

Opt-in because it creates billable machines:

    OMNIGENT_E2E_BOXD=1 BOXD_API_KEY=bxd_... [BOXD_ORG=my-org] \
    .venv/bin/python -m pytest tests/e2e/integrations/deploy/boxd/test_launcher_live.py -v
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from omnigent.onboarding.sandboxes.boxd import BoxdSandboxLauncher

if TYPE_CHECKING:
    from boxd import Boxd

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_BOXD") != "1",
        reason="set OMNIGENT_E2E_BOXD=1 to create live boxd machines",
    ),
    pytest.mark.timeout(900),
]

_MARKER_ENV = "OMNIGENT_E2E_BOXD_MARKER"


@pytest.fixture(scope="module")
def launcher() -> Iterator[BoxdSandboxLauncher]:
    """A launcher that forwards one marker variable, preflighted."""
    os.environ[_MARKER_ENV] = f"marker-{int(time.time())}"
    boxd_launcher = BoxdSandboxLauncher(env=[_MARKER_ENV])
    boxd_launcher.prepare()
    try:
        yield boxd_launcher
    finally:
        os.environ.pop(_MARKER_ENV, None)


@pytest.fixture(scope="module")
def machine(launcher: BoxdSandboxLauncher) -> Iterator[str]:
    """One provisioned machine shared by the module, deleted at the end."""
    machine_id = launcher.provision("e2e-launcher")
    try:
        yield machine_id
    finally:
        launcher.terminate(machine_id)


def test_provision_bootstraps_an_isolated_host(
    launcher: BoxdSandboxLauncher, machine: str
) -> None:
    """The machine carries the host dependencies and none of boxd's own tooling."""
    result = launcher.run(
        machine, "omnigent --version && command -v bwrap && git config --global credential.helper"
    )
    assert "omnigent" in result.stdout
    assert "/bwrap" in result.stdout
    assert "GIT_TOKEN" in result.stdout
    isolated = launcher.run(machine, "command -v boxd", check=False)
    assert isolated.returncode != 0, "isolated machines must not carry the in-VM boxd CLI"


def test_run_forwards_env_exit_codes_and_streams(
    launcher: BoxdSandboxLauncher, machine: str
) -> None:
    """Commands see the passthrough env; exit codes and stderr come back intact."""
    marker = os.environ[_MARKER_ENV]
    result = launcher.run(machine, f'printf "%s %s" "${_MARKER_ENV}" "$IS_SANDBOX"')
    assert result.stdout == f"{marker} 1"
    failed = launcher.run(machine, "echo out; echo err >&2; exit 42", check=False)
    assert (failed.returncode, failed.stdout, failed.stderr) == (42, "out\n", "err\n")


def test_put_round_trips_a_large_file(
    launcher: BoxdSandboxLauncher, machine: str, tmp_path: Path
) -> None:
    """A 5 MiB upload arrives byte for byte (above the 4 MiB unary message cap)."""
    payload = os.urandom(5 * 1024 * 1024 + 7)
    local = tmp_path / "payload.bin"
    local.write_bytes(payload)
    launcher.put(machine, local, "/tmp/payload.bin")
    remote_sha = launcher.run(machine, "sha256sum /tmp/payload.bin").stdout.split()[0]
    assert remote_sha == hashlib.sha256(payload).hexdigest()


def test_stream_exec_and_foreground(launcher: BoxdSandboxLauncher, machine: str) -> None:
    """Streaming merges both streams; the foreground attach reports the exit code."""
    process = launcher.stream_exec(machine, "echo out-line; echo err-line >&2; exit 3")
    lines = list(process.lines)
    assert process.wait() == 3
    assert "out-line\n" in lines and "err-line\n" in lines
    assert launcher.exec_foreground(machine, "sh -c 'test -t 1 && exit 7'") == 7


def _settle(client: Boxd, machine_id: str, status: str) -> None:
    """Wait until a sleep transition finishes; hibernation lands a few seconds late."""
    deadline = time.monotonic() + 60
    while client.machines.get(machine_id).status != status:
        assert time.monotonic() < deadline, f"machine never reached {status!r}"
        time.sleep(0.5)


def test_background_process_survives_sleep_and_resume(
    launcher: BoxdSandboxLauncher, machine: str
) -> None:
    """A background process outlives its exec and both sleep states; a stop keeps the disk."""
    from boxd import Boxd

    launcher.run_background(machine, "sleep 3600", log_path="/tmp/bg.log")
    launcher.run(machine, "echo kept > /home/boxd/state.txt")
    launcher.keep_alive(machine)
    with Boxd() as client:
        for put_to_sleep, status in (
            (client.machines.pause, "suspended"),
            (client.machines.hibernate, "hibernated"),
        ):
            put_to_sleep(machine)
            _settle(client, machine, status)
            assert launcher.is_running(machine) is False
            launcher.resume(machine)
            assert launcher.is_running(machine) is True
            assert "sleep 3600" in launcher.run(machine, "ps -eo args").stdout
        # A stop keeps the disk; whether processes survive is boxd's call.
        client.machines.stop(machine)
        _settle(client, machine, "stopped")
        assert launcher.is_running(machine) is False
        launcher.resume(machine)
    assert launcher.is_running(machine) is True
    assert launcher.run(machine, "cat /home/boxd/state.txt").stdout == "kept\n"


def test_terminate_is_idempotent(launcher: BoxdSandboxLauncher) -> None:
    """Deleting twice succeeds and the machine then reports not running."""
    machine_id = launcher.provision("e2e-terminate")
    launcher.terminate(machine_id)
    launcher.terminate(machine_id)
    # The delete returns before the record disappears (well under a second).
    deadline = time.monotonic() + 30
    while launcher.is_running(machine_id) is not False and time.monotonic() < deadline:
        time.sleep(0.5)
    assert launcher.is_running(machine_id) is False
