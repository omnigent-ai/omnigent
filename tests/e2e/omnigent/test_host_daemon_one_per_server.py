"""E2E: one server must be served by one host daemon across URL spellings.

Daemon registry records are keyed on the raw ``--server`` string, so one
local server reachable at one port can accrue several live host daemons —
``http://127.0.0.1:<port>`` and ``http://localhost:<port>`` each mint their
own record and daemon. Both daemons advertise the same machine-wide
``host_id``, so they fight over the server's registry slot (continuous
"replacing stale host connection" takeover churn), and
``omnigent host stop --server <url>`` stops only the daemon whose record
matches that exact spelling, leaving the other running.

These tests drive the real CLI against a real detached local server and
assert the user-visible invariants a fix must restore:

1. Starting a host with a second loopback spelling of an already-served
   server reuses the live daemon instead of spawning a duplicate.
2. ``host stop`` for any spelling of a server converges: no host daemon for
   that server survives it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _BOOT_TIMEOUT,
    _POLL_PAUSE,
    _connect_env,
    _force_stop_server,
    _read_local_server_record,
)

# Daemon teardown after `host stop` is asynchronous (SIGTERM + lifecycle
# guard); bounded poll budget for observing the processes disappear.
_CONVERGE_TIMEOUT = 30.0


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* names a live process (signal 0 probe).

    :param pid: Process id to probe, e.g. ``12345``.
    :returns: ``True`` if the process exists, ``False`` once it has exited.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _run_cli(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run one ``omnigent`` CLI command as the user would.

    :param omnigent_python: Python interpreter with omnigent installed.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Isolated subprocess environment from ``_connect_env``.
    :param args: CLI arguments after ``omnigent``, e.g. ``("host", "stop")``.
    :returns: The completed process with captured output.
    """
    return subprocess.run(
        [str(omnigent_python), "-m", "omnigent", *args],
        env=dict(env),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_BOOT_TIMEOUT,
    )


def _daemon_records(home: Path) -> list[dict]:
    """Read every daemon registry record under the isolated home.

    :param home: Isolated HOME holding ``.omnigent/daemons``.
    :returns: Parsed record dicts, sorted by path for stable output.
    """
    daemons = home / ".omnigent" / "daemons"
    if not daemons.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(daemons.glob("*.json"))]


def _live_daemons(home: Path) -> list[dict]:
    """Return the registry records whose daemon process is still alive.

    :param home: Isolated HOME holding ``.omnigent/daemons``.
    :returns: Records of live daemons.
    """
    return [r for r in _daemon_records(home) if _pid_alive(r["pid"])]


def _start_background_server(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
    home: Path,
) -> tuple[int, int]:
    """Start the detached local server and return its ``(pid, port)``.

    :param omnigent_python: Python interpreter with omnigent installed.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Isolated subprocess environment.
    :param home: Isolated HOME (the pidfile lands under ``<home>/.omnigent``).
    :returns: The detached server's pid and port.
    """
    proc = _run_cli(omnigent_python, repo_root, env, "server", "--background")
    assert proc.returncode == 0, (
        f"background server spawn failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    return _read_local_server_record(home)


def _spawn_host_daemon(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
    server_url: str,
) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent host --background --server <url>`` for one spelling.

    :param omnigent_python: Python interpreter with omnigent installed.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Isolated subprocess environment.
    :param server_url: The ``--server`` spelling to connect with.
    :returns: The completed spawn command.
    """
    proc = _run_cli(
        omnigent_python,
        repo_root,
        env,
        "host",
        "--background",
        "--non-interactive",
        "--server",
        server_url,
    )
    assert proc.returncode == 0, (
        f"host spawn for {server_url} failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    return proc


def _teardown(home: Path, server_pid: int) -> None:
    """Best-effort teardown of every daemon and the detached server.

    :param home: Isolated HOME holding the daemon registry.
    :param server_pid: Detached server pid, or ``-1`` when it never started.
    """
    for record in _daemon_records(home):
        if _pid_alive(record["pid"]):
            _force_stop_server(record["pid"])
    if server_pid > 0:
        _force_stop_server(server_pid)


def test_second_loopback_spelling_reuses_live_daemon(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A second loopback spelling of one server must not mint a second daemon.

    Starting ``omnigent host`` against ``http://127.0.0.1:<port>`` and then
    ``http://localhost:<port>`` targets the same server, so the second
    invocation must reuse the live daemon. Two concurrent daemons would
    present one ``host_id`` twice and churn the server's registry slot.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)
    server_pid = -1
    try:
        server_pid, port = _start_background_server(
            omnigent_python, omnigent_repo_root, env, home
        )
        _spawn_host_daemon(
            omnigent_python, omnigent_repo_root, env, f"http://127.0.0.1:{port}"
        )
        _spawn_host_daemon(
            omnigent_python, omnigent_repo_root, env, f"http://localhost:{port}"
        )

        live = _live_daemons(home)
        assert len(live) == 1, (
            f"one server (port {port}) must be served by one host daemon, but "
            f"two loopback spellings yielded {len(live)} live daemons: "
            + ", ".join(f"target={r['target']!r} pid={r['pid']}" for r in live)
        )
    finally:
        _teardown(home, server_pid)


def test_host_stop_converges_all_daemons_for_one_server(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """``host stop`` for one spelling must stop every daemon of that server.

    After hosts were started with two loopback spellings of the same server,
    ``omnigent host stop --server http://localhost:<port>`` must converge:
    no host daemon serving that server may survive it. Today the record for
    the other spelling is untouched and its daemon keeps running.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)
    server_pid = -1
    try:
        server_pid, port = _start_background_server(
            omnigent_python, omnigent_repo_root, env, home
        )
        _spawn_host_daemon(
            omnigent_python, omnigent_repo_root, env, f"http://127.0.0.1:{port}"
        )
        _spawn_host_daemon(
            omnigent_python, omnigent_repo_root, env, f"http://localhost:{port}"
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
            f"host stop failed (rc={stop.returncode}):\n{stop.stdout}\n{stop.stderr}"
        )

        # Teardown is asynchronous; give the stopped daemons a bounded window
        # to disappear before judging convergence.
        elapsed = 0.0
        while elapsed < _CONVERGE_TIMEOUT and _live_daemons(home):
            _POLL_PAUSE.wait(0.25)
            elapsed += 0.25

        survivors = _live_daemons(home)
        assert not survivors, (
            "`host stop` for one spelling of the server must converge every "
            f"daemon serving it, but these survived: "
            + ", ".join(f"target={r['target']!r} pid={r['pid']}" for r in survivors)
        )
    finally:
        _teardown(home, server_pid)
