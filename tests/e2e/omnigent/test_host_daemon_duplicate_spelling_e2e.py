"""E2E reproduction: one server instance must map to one host daemon.

A local Omnigent server is reachable under several loopback spellings
(``http://127.0.0.1:<port>``, ``http://localhost:<port>``), but daemon
registry records are keyed on the requested URL string, so each spelling
spawns its own host daemon instead of reusing the live one. Both daemons
present the same machine-wide ``host_id`` and fight over the server's
single registry slot (continuous ``replacing stale host connection``
takeover churn), and ``omnigent host stop --server <url>`` only stops the
daemon recorded under that exact spelling, so the machine never converges
back to zero daemons without ``pkill``.

This test drives the real user journey end-to-end against the real CLI:
start the background local server, connect a host daemon via the
``127.0.0.1`` spelling, connect again via the ``localhost`` spelling, and
require that the second command reuses the live daemon (one daemon process
for one server instance — which is also what prevents the host_id takeover
churn); then run ``omnigent host stop`` via the second spelling and require
that no daemon for this server survives it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _BOOT_TIMEOUT,
    _connect_env,
    _force_stop_server,
    _read_local_server_record,
    _wait_for_health,
)
from tests.e2e.omnigent.test_host_daemon_lifecycle_lock_e2e import (
    _pid_alive,
    _wait_pid_gone,
)

# Budget for daemons to exit after a successful stop; teardown is quick, but
# a loaded CI box still needs headroom.
_STOP_TIMEOUT = 30.0


def _run_cli(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run one ``omnigent`` CLI command to completion.

    :param omnigent_python: Python interpreter fixture.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Subprocess environment (isolated HOME) from ``_connect_env``.
    :param args: CLI arguments after ``omnigent``, e.g. ``("host", "stop")``.
    :returns: The completed process.
    """
    return subprocess.run(
        [str(omnigent_python), "-m", "omnigent", *args],
        env=dict(env),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_BOOT_TIMEOUT,
    )


def _live_daemon_records(home: Path) -> dict[str, int]:
    """Map daemon record target -> pid for records whose process is alive.

    :param home: Isolated HOME holding ``.omnigent/daemons``.
    :returns: ``{target: pid}`` for live daemons, e.g.
        ``{"http://127.0.0.1:6767": 1234}``.
    """
    daemons_dir = home / ".omnigent" / "daemons"
    live: dict[str, int] = {}
    if not daemons_dir.is_dir():
        return live
    for path in sorted(daemons_dir.glob("*.json")):
        data = json.loads(path.read_text())
        if _pid_alive(int(data["pid"])):
            live[str(data["target"])] = int(data["pid"])
    return live


def test_second_loopback_spelling_reuses_daemon_and_stop_converges(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A second loopback spelling reuses the daemon, and stop converges.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)

    daemon_pids: set[int] = set()
    server_pid = -1
    try:
        start = _run_cli(omnigent_python, omnigent_repo_root, env, "server", "--background")
        assert start.returncode == 0, (
            f"'omnigent server --background' failed (rc={start.returncode})\n"
            f"stdout:\n{start.stdout}\nstderr:\n{start.stderr}"
        )
        server_pid, port = _read_local_server_record(home)
        assert _wait_for_health(port, expected=True, timeout=_BOOT_TIMEOUT), (
            f"background local server on port {port} never became healthy"
        )

        first = _run_cli(
            omnigent_python,
            omnigent_repo_root,
            env,
            "host",
            "--background",
            "--non-interactive",
            "--server",
            f"http://127.0.0.1:{port}",
        )
        assert first.returncode == 0, (
            f"first host spawn failed (rc={first.returncode})\n"
            f"stdout:\n{first.stdout}\nstderr:\n{first.stderr}"
        )
        live = _live_daemon_records(home)
        daemon_pids |= set(live.values())
        assert len(live) == 1, f"expected exactly one daemon after the first spawn, got {live}"

        second = _run_cli(
            omnigent_python,
            omnigent_repo_root,
            env,
            "host",
            "--background",
            "--non-interactive",
            "--server",
            f"http://localhost:{port}",
        )
        assert second.returncode == 0, (
            f"second host spawn failed (rc={second.returncode})\n"
            f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
        )
        live = _live_daemon_records(home)
        daemon_pids |= set(live.values())
        assert len(live) == 1, (
            f"one server instance must be served by one host daemon, but the "
            f"localhost spelling spawned its own instead of reusing the live "
            f"127.0.0.1 one: {live}\nstdout:\n{second.stdout}"
        )

        stop = _run_cli(
            omnigent_python,
            omnigent_repo_root,
            env,
            "host",
            "stop",
            "--server",
            f"http://localhost:{port}",
        )
        assert stop.returncode == 0, (
            f"'omnigent host stop' failed (rc={stop.returncode})\n"
            f"stdout:\n{stop.stdout}\nstderr:\n{stop.stderr}"
        )
        for pid in sorted(daemon_pids):
            assert _wait_pid_gone(pid, timeout=_STOP_TIMEOUT), (
                f"daemon pid {pid} survived 'omnigent host stop --server "
                f"http://localhost:{port}' — stop did not converge for this "
                f"server instance"
            )
        leftovers = _live_daemon_records(home)
        assert not leftovers, f"live daemons left after stop: {leftovers}"
    finally:
        for pid in sorted(daemon_pids):
            if pid > 0 and _pid_alive(pid):
                _force_stop_server(pid)
        if server_pid > 0 and _pid_alive(server_pid):
            _force_stop_server(server_pid)
