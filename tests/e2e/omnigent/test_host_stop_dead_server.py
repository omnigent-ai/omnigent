"""E2E reproduction: ``omnigent host stop`` must stop a daemon whose server died.

``omnigent host --background ""`` (local mode) spawns a detached background
local AP server plus a host daemon bound to it. When that server later dies
out from under the daemon (a crash, an OOM kill, a reboot race), a plain
``omnigent host stop`` first asks the dead server for its session list, hits
``ConnectError: Connection refused``, and aborts with a non-zero exit --
leaving the daemon process and its registry record in place. The user's stop
command fails and the stale daemon lingers until they discover ``--force``.

This test drives the real user journey end-to-end: boot the background
daemon, SIGKILL the detached server (the crash), then run the plain
``omnigent host stop`` and require the default stop to still succeed -- exit
zero, the daemon process gone, and its registry record removed -- without the
user having to reach for ``--force``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _BOOT_TIMEOUT,
    _connect_env,
    _force_stop_server,
    _read_local_server_record,
)
from tests.e2e.omnigent.test_host_daemon_lifecycle_lock_e2e import (
    _pid_alive,
    _spawn_background_daemon,
    _wait_for_daemon_record,
    _wait_pid_gone,
)

# Budget for the daemon to exit after a successful stop. The stop command
# deletes the record and signals the daemon; teardown is quick, but a loaded
# CI box still needs headroom.
_STOP_TIMEOUT = 30.0


def test_host_stop_succeeds_after_local_server_death(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A plain ``host stop`` stops the daemon even when its server is dead.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)

    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"

    daemon_pid = -1
    server_pid = -1
    record = None
    try:
        record = _wait_for_daemon_record(home / ".omnigent" / "daemons", timeout=_BOOT_TIMEOUT)
        daemon_pid = json.loads(record.read_text())["pid"]
        server_pid, _port = _read_local_server_record(home)
        assert _pid_alive(daemon_pid), "background daemon should be alive after spawn"
        assert _pid_alive(server_pid), "detached local server should be alive after spawn"

        # The crash: the detached local server dies out from under the daemon.
        os.kill(server_pid, signal.SIGKILL)
        assert _wait_pid_gone(server_pid, timeout=_STOP_TIMEOUT), (
            f"detached server pid {server_pid} survived SIGKILL"
        )

        stop = subprocess.run(
            [str(omnigent_python), "-m", "omnigent", "host", "stop"],
            env=dict(env),
            cwd=str(omnigent_repo_root),
            capture_output=True,
            text=True,
            timeout=_BOOT_TIMEOUT,
        )

        # The default stop must tolerate the dead server and still stop the
        # daemon. The reported failure mode is exactly this branch: the
        # session-list preflight hits Connection refused, the command exits
        # non-zero telling the user to retry with --force, and the daemon
        # (plus its registry record) is left behind.
        assert stop.returncode == 0, (
            f"plain 'omnigent host stop' failed against a dead local server "
            f"(rc={stop.returncode})\nstdout:\n{stop.stdout}\nstderr:\n{stop.stderr}"
        )
        assert _wait_pid_gone(daemon_pid, timeout=_STOP_TIMEOUT), (
            f"host daemon pid {daemon_pid} was still alive after 'omnigent host stop' succeeded"
        )
        assert not record.exists(), (
            f"daemon registry record {record} was left behind after a successful stop"
        )
    finally:
        for pid in (daemon_pid, server_pid):
            if pid > 0 and _pid_alive(pid):
                _force_stop_server(pid)
