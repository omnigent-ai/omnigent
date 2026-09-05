"""End-to-end regression test: pytest must not leave Omnigent processes behind.

Reproduces the developer journey from the bug report "pytest leaves Omnigent
servers and host daemons running after the suite exits":

1. run pytest on a CLI test that spawns a real detached Omnigent child,
2. pytest exits (green) and removes its temp ``OMNIGENT_DATA_DIR``,
3. ``ps`` still shows ``omnigent.host._daemon_entry`` / ``omnigent.cli
   server`` / ``omnigent.runner._zygote`` processes spawned by the run —
   orphans that squat port 6767 (so a later ``omni start`` silently falls
   back to a random port) and keep serving with their database directory
   gone.

The nested run drives the exact leak seam from the report: an ``omnigent
claude --server <url>`` invocation that stubs the native launcher but not
``_ensure_backend``, so the Click command's ``_ensure_host_daemon`` really
Popens ``python -m omnigent.host._daemon_entry`` with
``start_new_session=True``. The target URL is a loopback port nothing
listens on — connection-refused keeps the daemon in its retry loop for
minutes (``_LOOPBACK_REFUSED_FATAL_ATTEMPTS``), so a surviving orphan is
observable deterministically, with no dependence on external-network shape
(the suite's real leaking tests point at ``https://example.com``, whose
orphan lifetime varies with how fast that host answers).

The nested pytest loads ``tests/conftest.py`` as a plugin, so it exercises
the session-level spawn/teardown wiring under test: the conftest points
``OMNIGENT_DATA_DIR`` at a fresh ``mkdtemp(prefix="omnigent-pytest-")``
(honoring ``TMPDIR``), and its ``pytest_unconfigure`` owns reaping.
Confining the nested run to a private ``TMPDIR`` lets survivors be matched
by their environment or command line without ever touching unrelated
processes.

No LLM is needed — this is pure process-lifecycle wiring — so it runs
without ``--llm-api-key``::

    .venv/bin/python -m pytest tests/e2e/test_pytest_process_leak_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The nested leaky test: drives the real `omnigent claude` Click command
# with only the native launcher and config loading stubbed — the same
# seam as the suite's real leaking tests (e.g. the `omnigent claude
# --resume` parsing tests) — so `_ensure_backend` really spawns a
# detached `omnigent.host._daemon_entry` child. The daemon target comes
# from the environment: a loopback port with no listener, so the daemon
# survives in its connection-refused retry loop unless the session
# teardown under test reaps it.
_NESTED_LEAKY_TEST = '''\
"""Nested probe: a CLI test whose command spawns a real detached daemon."""

import os

from click.testing import CliRunner

from omnigent.cli import cli


def test_claude_command_spawns_detached_host_daemon(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        cli,
        ["claude", "--server", os.environ["LEAK_PROBE_SERVER_URL"], "--", "-p", "hi"],
    )

    assert result.exit_code == 0, result.output
    assert captured["server"] == os.environ["LEAK_PROBE_SERVER_URL"]
'''

# Budget for the nested pytest run: one test, but a cold interpreter that
# imports the full CLI stack first.
_NESTED_PYTEST_TIMEOUT_S = 300.0
# Grace period after nested pytest exits before scanning: long enough for
# a child that IS being torn down to disappear, short enough to keep the
# test fast. The leak is not a race — a refused-loopback daemon retries
# for ~5 minutes, and orphans in the report survived a full day.
_POST_EXIT_SETTLE_S = 3.0
_REAP_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.25


def _free_loopback_port() -> int:
    """Reserve then release a loopback port, returning its number.

    Nothing listens on the returned port, so a daemon pointed at it gets
    connection-refused — the retriable failure that keeps it alive.

    :returns: A loopback TCP port with no listener.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _proc_env(proc: psutil.Process) -> dict[str, str]:
    """Return a process's environment, or ``{}`` when unreadable.

    :param proc: The process to inspect.
    :returns: Its environment dict; empty for zombies/foreign processes.
    """
    try:
        return proc.environ()
    except (psutil.Error, OSError):
        return {}


def _proc_cmdline(proc: psutil.Process) -> str:
    """Return a process's command line as one string, or ``""``.

    :param proc: The process to inspect.
    :returns: Space-joined argv; empty when unreadable.
    """
    try:
        return " ".join(proc.cmdline())
    except (psutil.Error, OSError):
        return ""


def _surviving_omnigent_procs(tmp_root: Path, nested_pid: int) -> list[tuple[int, str]]:
    """Find live Omnigent processes attributable to the nested pytest run.

    A survivor is attributed by the private ``TMPDIR`` the nested run was
    confined to: its ``OMNIGENT_DATA_DIR`` env (inherited by host daemons
    and servers via the spawn-env allowlists) or its command line (a
    spawned server's ``--database-uri sqlite:///<data_dir>/chat.db``)
    references a path under ``tmp_root``. The nested pytest process itself
    (and anything still parented to it) is excluded — only processes that
    OUTLIVED pytest count as leaks.

    :param tmp_root: The private ``TMPDIR`` the nested run used.
    :param nested_pid: The nested pytest's pid, excluded from the scan.
    :returns: ``(pid, cmdline)`` pairs of surviving Omnigent processes.
    """
    tmp_prefix = f"{tmp_root}{os.sep}"
    survivors: list[tuple[int, str]] = []
    for proc in psutil.process_iter(["pid"]):
        if proc.pid in (nested_pid, os.getpid()):
            continue
        cmdline = _proc_cmdline(proc)
        if "omnigent" not in cmdline:
            continue
        env = _proc_env(proc)
        data_dir = env.get("OMNIGENT_DATA_DIR", "")
        if not (data_dir.startswith(tmp_prefix) or tmp_prefix in cmdline):
            continue
        survivors.append((proc.pid, cmdline))
    return survivors


def _reap(pid: int) -> None:
    """Terminate a leaked (detached, non-child) process: TERM then KILL.

    :param pid: The orphan's process id.
    """
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _REAP_TIMEOUT_S
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        time.sleep(_POLL_INTERVAL_S)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def test_pytest_run_leaves_no_omnigent_processes(tmp_path: Path) -> None:
    """A pytest session must reap every Omnigent child its tests spawned.

    Drives the real journey: run pytest on a CLI test that spawns a
    detached Omnigent host daemon, let pytest exit, then assert nothing
    from the run is still alive. Without session-teardown reaping this
    FAILS: the run leaves a ``python -m omnigent.host._daemon_entry``
    orphan behind (and, on runs that exercise the local-backend path,
    ``omnigent.cli server`` / ``omnigent.runner._zygote`` orphans too) —
    free to squat port 6767 and to keep serving after its temp data dir
    is deleted.
    """
    tmp_root = tmp_path / "nested-tmp"
    tmp_root.mkdir()
    nested_test = tmp_path / "leak_probe_test.py"
    nested_test.write_text(_NESTED_LEAKY_TEST, encoding="utf-8")

    env = os.environ.copy()
    # Confine the nested run's mkdtemp'd OMNIGENT_DATA_DIR to a private
    # TMPDIR so survivors are attributable to THIS run and nothing else.
    env["TMPDIR"] = str(tmp_root)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    # A loopback port with no listener: connection-refused keeps the
    # spawned daemon retrying (alive) instead of exiting on a permanent
    # error, so the orphan is deterministically observable.
    env["LEAK_PROBE_SERVER_URL"] = f"http://127.0.0.1:{_free_loopback_port()}"

    nested = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            str(nested_test),
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            # The probe file lives outside the repo tree, so pytest would
            # not discover tests/conftest.py for it. Load it explicitly:
            # it is the session-level spawn/teardown wiring under test
            # (OMNIGENT_DATA_DIR isolation + unconfigure cleanup).
            "-p",
            "tests.conftest",
        ],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    survivors: list[tuple[int, str]] = []
    try:
        stdout, _ = nested.communicate(timeout=_NESTED_PYTEST_TIMEOUT_S)
        output = stdout.decode(errors="replace")
        # The nested test itself must have run and passed: a failing
        # probe means this regression test is probing nothing.
        assert nested.returncode == 0, (
            f"nested pytest of the daemon-spawning probe failed "
            f"(rc={nested.returncode}).\n--- nested output ---\n{output}"
        )

        time.sleep(_POST_EXIT_SETTLE_S)
        survivors = _surviving_omnigent_procs(tmp_root, nested.pid)

        leaked = "\n".join(f"  pid {pid}: {cmd}" for pid, cmd in survivors)
        assert not survivors, (
            "pytest exited but left Omnigent processes from its run alive "
            "(session teardown never reaped spawned children — they squat "
            "port 6767 and keep serving after their temp data dir is "
            f"deleted):\n{leaked}"
        )
    finally:
        nested_alive = nested.poll() is None
        if nested_alive:
            nested.kill()
            nested.wait(timeout=_REAP_TIMEOUT_S)
        for pid, _ in survivors or _surviving_omnigent_procs(tmp_root, nested.pid):
            _reap(pid)
