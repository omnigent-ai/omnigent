"""E2E coverage for the host daemon lifecycle-lock self-termination guard.

A running host daemon binds its lifetime to its registry record: it holds an
exclusive ``flock`` on ``~/.omnigent/daemons/<hash>.json`` and watches that
same file. When the record is deleted (``omnigent host stop``) or its ``pid``
is reassigned (a newer daemon claimed the target), the daemon retires itself
instead of lingering as a stale process.

The guard is exercised against both daemon flavors:

- The **foreground** ``omnigent host ""`` daemon, driven under a PTY. Local
  mode spawns a detached server, so a clean return from the run loop surfaces
  the ``Stop it too?`` prompt — its appearance is end-to-end proof that the
  guard fired and broke the run loop (a daemon that ignored the record would
  keep serving and never prompt).
- The **detached background** ``omnigent host --background ""`` daemon (spawned
  via ``omnigent.host._daemon_entry``) — the real stale-daemon case. There is
  no terminal, so the test observes the daemon *process* itself: it must die
  after the record is mutated, and its flock must then be free.

Each flavor is tested for both self-terminate triggers: the record being
deleted, and its ``pid`` being reassigned to a foreign owner.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

import pexpect

from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _BOOT_TIMEOUT,
    _EXIT_TIMEOUT,
    _LEFT_RUNNING_MARKER,
    _POLL_PAUSE,
    _PROMPT_MARKER,
    _boot_connect_and_get_server,
    _connect_env,
    _force_stop_server,
    _read_local_server_record,
    _server_healthy,
    _spawn_connect,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock.
    fcntl = None  # type: ignore[assignment]

# Production polls the lifecycle record every 60s; drive it fast here so the
# self-terminate path is observable without a minute-long wait per test.
_FAST_LIFECYCLE_POLL_S = "1"

# A few fast poll cycles plus daemon teardown; generous so a loaded CI box
# still observes the self-termination.
_SELF_TERMINATE_TIMEOUT = 30.0
_PROMPT_TIMEOUT = 30.0


def _lifecycle_env(base_env: dict[str, str], home: Path) -> dict[str, str]:
    """Isolated subprocess env with the lifecycle monitor polling fast.

    ``OMNIGENT_HOST_LIFECYCLE_POLL_S`` is on the daemon env allowlist (the
    ``OMNIGENT_`` prefix), so it reaches the detached background daemon too.

    :param base_env: Fixture credential environment.
    :param home: Isolated HOME for this run.
    :returns: Environment dict for the CLI/daemon subprocess.
    """
    env = _connect_env(base_env, home)
    env["OMNIGENT_HOST_LIFECYCLE_POLL_S"] = _FAST_LIFECYCLE_POLL_S
    return env


def _wait_for_daemon_record(daemons_dir: Path, *, timeout: float) -> Path:
    """Wait for the daemon to write its record, then return its path.

    :param daemons_dir: ``<home>/.omnigent/daemons`` for the isolated run.
    :param timeout: Max seconds to poll for the record to appear.
    :returns: The ``<hash>.json`` record path for the (single) local daemon.
    :raises AssertionError: If no record appears within *timeout*.
    """
    elapsed = 0.0
    while elapsed < timeout:
        records = sorted(daemons_dir.glob("*.json")) if daemons_dir.is_dir() else []
        if records:
            return records[0]
        _POLL_PAUSE.wait(0.25)
        elapsed += 0.25
    raise AssertionError(f"daemon record never appeared under {daemons_dir}")


def _lock_is_held(record_path: Path) -> bool:
    """Return whether another process holds the exclusive flock on the record.

    :param record_path: The daemon's ``<hash>.json`` record file.
    :returns: ``True`` if a non-blocking exclusive lock attempt is refused
        (i.e. the daemon holds it); ``False`` if we could take it ourselves or
        the record is gone (a deleted record obviously holds no lock).
    """
    assert fcntl is not None, "flock unavailable on this platform"
    try:
        fd = os.open(record_path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        # We took it — the daemon is not holding it. Release before reporting.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _assert_daemon_owns_record(record: Path, pid: int) -> None:
    """Assert the daemon holds the record's flock and the record names its pid.

    :param record: The daemon's JSON registry record (also the flocked file).
    :param pid: The daemon process id.
    """
    assert _lock_is_held(record), f"daemon did not hold the flock on {record}"
    assert json.loads(record.read_text())["pid"] == pid, "record does not name the daemon pid"


def _drive_self_termination(
    child: pexpect.spawn,
    home: Path,
    port: int,
    mutate: str,
) -> None:
    """Mutate the daemon's record, then assert it self-terminates cleanly.

    :param child: Booted ``omnigent host`` pexpect child (the daemon itself).
    :param home: Isolated HOME holding the daemon registry.
    :param port: Detached local server port (asserted still up at the end).
    :param mutate: ``"delete"`` to unlink the record, ``"reassign"`` to
        rewrite it with a foreign pid.
    """
    daemons = home / ".omnigent" / "daemons"
    record = _wait_for_daemon_record(daemons, timeout=_BOOT_TIMEOUT)
    _assert_daemon_owns_record(record, child.pid)

    # The monitor self-terminates only after it has confirmed ownership at
    # least once (the startup-grace latch). One poll cycle guarantees that.
    _POLL_PAUSE.wait(6.0)

    if mutate == "delete":
        record.unlink()
    else:
        payload = json.loads(record.read_text())
        payload["pid"] = 999_999  # a pid this daemon can never be
        record.write_text(json.dumps(payload))

    # Clean return from the run loop surfaces the local-mode stop prompt.
    child.expect(_PROMPT_MARKER, timeout=_SELF_TERMINATE_TIMEOUT)
    child.send("n\r")
    child.expect(_LEFT_RUNNING_MARKER, timeout=_PROMPT_TIMEOUT)
    child.expect(pexpect.EOF, timeout=_EXIT_TIMEOUT)
    assert _server_healthy(port), "detached server should survive the daemon retiring"


def test_host_daemon_self_terminates_when_record_deleted(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Deleting the registry record retires the running daemon.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _lifecycle_env(mock_credentials_env, home)
    child = _spawn_connect(omnigent_python, omnigent_repo_root, env)
    server_pid = -1
    try:
        server_pid, port = _boot_connect_and_get_server(child, home)
        _drive_self_termination(child, home, port, mutate="delete")
    finally:
        if server_pid > 0:
            _force_stop_server(server_pid)
        if not child.closed:
            child.close(force=True)


def test_host_daemon_self_terminates_when_record_reassigned(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Reassigning the record's pid (a newer owner) retires the running daemon.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _lifecycle_env(mock_credentials_env, home)
    child = _spawn_connect(omnigent_python, omnigent_repo_root, env)
    server_pid = -1
    try:
        server_pid, port = _boot_connect_and_get_server(child, home)
        _drive_self_termination(child, home, port, mutate="reassign")
    finally:
        if server_pid > 0:
            _force_stop_server(server_pid)
        if not child.closed:
            with contextlib.suppress(Exception):
                child.close(force=True)


# ── Detached background daemon (``omnigent host --background``) ──────────────


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* names a live process (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_pid_gone(pid: int, *, timeout: float) -> bool:
    """Poll until *pid* is no longer alive, up to *timeout* seconds."""
    elapsed = 0.0
    while elapsed < timeout:
        if not _pid_alive(pid):
            return True
        _POLL_PAUSE.wait(0.25)
        elapsed += 0.25
    return not _pid_alive(pid)


def _spawn_background_daemon(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent host --background ""`` (spawns a detached local daemon).

    :param omnigent_python: Python interpreter fixture.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Subprocess environment (isolated HOME) from ``_connect_env``.
    :returns: The completed process (returns once the daemon has registered).
    """
    return subprocess.run(
        [str(omnigent_python), "-m", "omnigent", "host", "--background", ""],
        env=dict(env),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_BOOT_TIMEOUT,
    )


def _drive_background_self_termination(home: Path, mutate: str) -> None:
    """Spawn a detached daemon, mutate its record, and assert it dies.

    :param home: Isolated HOME holding the daemon registry + server pidfile.
    :param mutate: ``"delete"`` to unlink the record, ``"reassign"`` to rewrite
        it with a foreign pid.
    """
    daemons = home / ".omnigent" / "daemons"
    record = _wait_for_daemon_record(daemons, timeout=_BOOT_TIMEOUT)
    daemon_pid = json.loads(record.read_text())["pid"]

    assert _pid_alive(daemon_pid), "background daemon should be alive after spawn"
    _assert_daemon_owns_record(record, daemon_pid)

    # Confirm-ownership latch: one poll cycle before the guard will act.
    _POLL_PAUSE.wait(6.0)

    if mutate == "delete":
        record.unlink()
    else:
        payload = json.loads(record.read_text())
        payload["pid"] = 999_999
        record.write_text(json.dumps(payload))

    assert _wait_pid_gone(daemon_pid, timeout=_SELF_TERMINATE_TIMEOUT), (
        f"detached daemon pid {daemon_pid} did not self-terminate after the record was {mutate}d"
    )
    # The dead daemon's flock must be free for the next owner to take (a
    # deleted record trivially holds none; a reassigned one must be unlocked).
    assert not _lock_is_held(record), "flock was not released after the daemon exited"


def _run_background_lifecycle_test(
    omnigent_python: Path,
    repo_root: Path,
    env: dict[str, str],
    home: Path,
    mutate: str,
) -> None:
    """Boot a detached daemon + server, drive self-termination, then clean up."""
    proc = _spawn_background_daemon(omnigent_python, repo_root, env)
    assert proc.returncode == 0, f"background spawn failed (rc={proc.returncode}):\n{proc.stderr}"
    assert "background" in proc.stdout.lower(), f"unexpected spawn output:\n{proc.stdout}"

    server_pid = -1
    daemon_pid = -1
    try:
        # The detached server outlives the daemon; capture its pid/port so the
        # assertion can confirm it survived and teardown can stop it.
        server_pid, port = _read_local_server_record(home)
        record = next((home / ".omnigent" / "daemons").glob("*.json"))
        daemon_pid = json.loads(record.read_text())["pid"]

        _drive_background_self_termination(home, mutate)

        assert _server_healthy(port), "detached server should survive the daemon retiring"
    finally:
        if daemon_pid > 0 and _pid_alive(daemon_pid):
            _force_stop_server(daemon_pid)
        if server_pid > 0:
            _force_stop_server(server_pid)


def test_background_daemon_self_terminates_when_record_deleted(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A detached ``--background`` daemon dies when its record is deleted.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _lifecycle_env(mock_credentials_env, home)
    _run_background_lifecycle_test(omnigent_python, omnigent_repo_root, env, home, "delete")


def test_background_daemon_self_terminates_when_record_reassigned(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A detached ``--background`` daemon dies when its record pid is reassigned.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _lifecycle_env(mock_credentials_env, home)
    _run_background_lifecycle_test(omnigent_python, omnigent_repo_root, env, home, "reassign")


def test_background_spawn_reuses_daemon_when_flock_held(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A second spin-up reuses the live daemon whose record flock is held.

    The first ``--background`` spawn leaves a daemon holding its record's
    flock. A second spawn must probe that held lock, conclude the owner is
    alive, and reuse it — same pid, no duplicate daemon.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    """
    home = tmp_path / "home"
    env = _lifecycle_env(mock_credentials_env, home)
    proc1 = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc1.returncode == 0, f"first spawn failed (rc={proc1.returncode}):\n{proc1.stderr}"

    daemons = home / ".omnigent" / "daemons"
    record = _wait_for_daemon_record(daemons, timeout=_BOOT_TIMEOUT)
    pid1 = json.loads(record.read_text())["pid"]
    server_pid = -1
    try:
        server_pid, _port = _read_local_server_record(home)
        assert _lock_is_held(record), "first daemon should hold the record flock"

        proc2 = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
        assert proc2.returncode == 0, (
            f"second spawn failed (rc={proc2.returncode}):\n{proc2.stderr}"
        )
        pid2 = json.loads(record.read_text())["pid"]
        assert pid2 == pid1, (
            f"flock-held daemon should be reused, not duplicated (pid1={pid1}, pid2={pid2})"
        )
        assert "already running" in proc2.stdout.lower(), (
            f"second spawn should report reuse, got:\n{proc2.stdout}"
        )
    finally:
        if pid1 > 0 and _pid_alive(pid1):
            _force_stop_server(pid1)
        if server_pid > 0:
            _force_stop_server(server_pid)
