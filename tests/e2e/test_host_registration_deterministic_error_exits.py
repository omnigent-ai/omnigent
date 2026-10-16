"""A host renamed onto another registered host's name must exit, not reconnect forever."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ONLINE_TIMEOUT_S = 60.0
_VERDICT_TIMEOUT_S = 45.0
# Reconnects citing the same registration error before the loop is judged
# permanent; the cadence is 0.5s -> ~1.2s -> ~2.6s -> 3.0s cap.
_LOOP_ANNOUNCEMENTS = 3

_REGISTRATION_FAILURE = "Host connection failed during registration"
_DELAY_RE = re.compile(r"Reconnecting in ([0-9.]+)s")


def _write_identity(home: Path, *, host_id: str, name: str) -> None:
    config_home = home / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": name}}, sort_keys=True)
    )


def _spawn_omnigent_host(
    home: Path, server_url: str
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """Run the user's ``omnigent host --server <url>`` with *home* as its identity."""
    console_log = home / "console.log"
    process_log = home / "host.log"
    # Compat mode: apply_runner_env strips the worktree PYTHONPATH so the pinned build wins.
    env = apply_runner_env(
        {
            **os.environ,
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(home / "config"),
            "OMNIGENT_DATA_DIR": str(home / "data"),
            PROCESS_LOG_FILE_ENV_VAR: str(process_log),
            "PYTHONPATH": os.pathsep.join(
                [str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
        }
    )
    with console_log.open("wb") as console:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                server_url,
                "--non-interactive",
            ],
            env=env,
            cwd=compat_runner_cwd() or str(_REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=console,
            stderr=subprocess.STDOUT,
        )
    return proc, console_log, process_log


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else "<missing>"


def _host_status(client: httpx.Client, host_id: str) -> str | None:
    resp = client.get("/v1/hosts")
    resp.raise_for_status()
    for host in resp.json().get("hosts", []):
        if host["host_id"] == host_id:
            return host["status"]
    return None


def _wait_for_host_online(
    client: httpx.Client,
    host_id: str,
    proc: subprocess.Popen[bytes],
    console_log: Path,
    process_log: Path,
) -> None:
    deadline = time.monotonic() + _ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        assert proc.poll() is None, (
            f"omnigent host exited early (code {proc.returncode}) while registering "
            f"{host_id}:\n{_read(console_log)[-2000:]}\n{_read(process_log)[-2000:]}"
        )
        if _host_status(client, host_id) == "online":
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"host {host_id} did not come online within {_ONLINE_TIMEOUT_S:.0f}s:\n"
        f"{_read(console_log)[-2000:]}\n{_read(process_log)[-2000:]}"
    )


def _wait_for_host_offline(
    client: httpx.Client, host_id: str, proc: subprocess.Popen[bytes]
) -> None:
    deadline = time.monotonic() + _ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        if _host_status(client, host_id) != "online":
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"host {host_id} still reported online {_ONLINE_TIMEOUT_S:.0f}s after being stopped "
        f"(process exit code: {proc.poll()})"
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


@pytest.mark.min_server_version("0.18.0")
# The host needs the host.connection_error frame to exit on a fatal refusal.
@pytest.mark.min_runner_version("0.11.0")
@pytest.mark.timeout(300)
def test_renamed_host_colliding_with_registered_name_fails_loudly(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A registration error that cannot recover must exit the host, not loop."""
    shared_name = f"e2e-shared-{uuid.uuid4().hex[:8]}"
    other_name = f"e2e-other-{uuid.uuid4().hex[:8]}"
    host_a, host_b = tmp_path / "host-a", tmp_path / "host-b"
    host_a_id, host_b_id = uuid.uuid4().hex, uuid.uuid4().hex
    _write_identity(host_a, host_id=host_a_id, name=shared_name)
    _write_identity(host_b, host_id=host_b_id, name=other_name)

    procs: list[subprocess.Popen[bytes]] = []
    try:
        for home, host_id in ((host_a, host_a_id), (host_b, host_b_id)):
            proc, console_log, process_log = _spawn_omnigent_host(home, live_server)
            procs.append(proc)
            _wait_for_host_online(http_client, host_id, proc, console_log, process_log)
            _stop(proc)
            _wait_for_host_offline(http_client, host_id, proc)

        _write_identity(host_b, host_id=host_b_id, name=shared_name)
        # The host appends to its process log; drop the clean first run so the
        # verdict scan only sees the collision run.
        (host_b / "host.log").unlink(missing_ok=True)
        proc, console_log, process_log = _spawn_omnigent_host(host_b, live_server)
        procs.append(proc)

        deadline = time.monotonic() + _VERDICT_TIMEOUT_S
        while proc.poll() is None and time.monotonic() < deadline:
            log_text = _read(process_log)
            if log_text.count(_REGISTRATION_FAILURE) >= _LOOP_ANNOUNCEMENTS:
                delays = _DELAY_RE.findall(log_text)
                raise AssertionError(
                    f"omnigent host is still running and has reconnected {len(delays)} "
                    f"times (announced delays {delays}) into the same deterministic "
                    f"registration error instead of exiting with code "
                    f"{HOST_FATAL_EXIT_CODE}. Host log tail:\n{log_text[-2500:]}"
                )
            time.sleep(POLL_INTERVAL_S)

        assert proc.poll() is not None, (
            f"omnigent host neither exited nor reported the registration failure within "
            f"{_VERDICT_TIMEOUT_S:.0f}s:\n{_read(console_log)[-2000:]}\n{_read(process_log)[-2000:]}"
        )
        console_text = _read(console_log)
        assert proc.returncode == HOST_FATAL_EXIT_CODE, (
            f"omnigent host exited with code {proc.returncode}, expected "
            f"HOST_FATAL_EXIT_CODE={HOST_FATAL_EXIT_CODE}:\n{console_text[-2000:]}"
        )
        log_text = _read(process_log)
        assert "Reconnecting in" not in log_text, (
            f"omnigent host reconnected before exiting on a deterministic registration "
            f"error:\n{log_text[-2500:]}"
        )
        assert "Could not connect" in console_text, (
            f"omnigent host exited without the loud failure banner:\n{console_text[-2000:]}"
        )
        failure_text = console_text.split("Could not connect", 1)[1]
        assert (
            "Host connection failed during registration" in failure_text
            and shared_name in failure_text
        ), (
            f"the failure banner does not carry the server's registration error:\n"
            f"{console_text[-2000:]}"
        )
        assert _host_status(http_client, host_b_id) != "online"
    finally:
        for proc in procs:
            _stop(proc)
