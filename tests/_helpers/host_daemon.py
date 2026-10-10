"""Spawn an isolated host daemon against a live server for e2e tests."""

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
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import (
    apply_runner_env,
    compat_runner_cwd,
    compat_runner_python,
    runner_executable,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The daemon logs this line when it spawns a runner subprocess.
_LAUNCH_LINE = re.compile(r"Launched runner (\S+) for workspace .*?\(pid=(\d+)\)")


def spawn_host_daemon(
    tmp_path: Path,
    server_url: str,
    mock_llm_url: str,
    *,
    runner_idle_timeout_s: float | None = None,
    pythonpath: str | None = None,
    zygote: bool = True,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Start a host with a fresh ``host_id``; return ``(process, host_id, daemon_log)``.

    A *pythonpath* fault must land in the runner process itself, so it needs
    ``zygote=False`` and a run that is not pinned to an older runner build.
    """
    if pythonpath is not None:
        if zygote:
            raise ValueError("a PYTHONPATH fault needs direct runner spawns; pass zygote=False")
        if compat_runner_python() is not None:
            raise RuntimeError(
                "a PYTHONPATH fault would shadow the pinned runner build; "
                "gate the test with min_runner_version"
            )
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    config: dict[str, object] = {"host": {"host_id": host_id, "name": f"test-host-{host_id[:8]}"}}
    if runner_idle_timeout_s is not None:
        config["runner"] = {"idle_timeout_s": runner_idle_timeout_s}
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(config, default_flow_style=False, sort_keys=True)
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = apply_runner_env(
        {
            **os.environ,
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
        }
    )
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    elif compat_runner_python() is None:
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    if not zygote:
        env["OMNIGENT_RUNNER_ZYGOTE"] = "0"
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", server_url],
            env=env,
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def wait_for_host_online(base_url: str, host_id: str, timeout: float = 45.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* shows online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # The server may still be starting; a transport error, a non-JSON body or a
        # partial host record is just another poll.
        with contextlib.suppress(httpx.HTTPError, ValueError, KeyError):
            resp = httpx.get(f"{base_url}/v1/hosts", timeout=5.0)
            if resp.status_code == 200 and any(
                host.get("host_id") == host_id and host.get("status") == "online"
                for host in resp.json().get("hosts", [])
            ):
                return
        time.sleep(0.25)
    raise AssertionError(f"host {host_id!r} never came online at {base_url}")


def launched_runners(daemon_log: Path) -> list[tuple[str, int]]:
    """Return every ``(runner_id, pid)`` the daemon spawned, in launch order."""
    if not daemon_log.exists():
        return []
    return [
        (runner_id, int(pid))
        for runner_id, pid in _LAUNCH_LINE.findall(daemon_log.read_text(errors="replace"))
    ]


def await_launched_runner(daemon_log: Path, *, timeout: float = 15.0) -> tuple[str, int]:
    """Return the first ``(runner_id, pid)`` the daemon logs within *timeout*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        launches = launched_runners(daemon_log)
        if launches:
            return launches[0]
        time.sleep(0.2)
    raise AssertionError("daemon never logged a runner launch")


def pid_alive(pid: int) -> bool:
    """Return whether *pid* still exists; a permission error means it does."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_host_daemon(daemon: subprocess.Popen[bytes] | None) -> None:
    """SIGTERM the daemon, escalating to SIGKILL after ten seconds."""
    if daemon is None:
        return
    daemon.send_signal(signal.SIGTERM)
    try:
        daemon.wait(timeout=10)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait()
