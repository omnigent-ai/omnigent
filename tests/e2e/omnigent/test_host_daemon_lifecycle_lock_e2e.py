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
import signal
import subprocess
import time
from pathlib import Path

import pexpect
import pytest

from omnigent.host.daemon_launch import HOST_DAEMON_COMMAND_ENV_VAR
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
    child.expect_exact(_PROMPT_MARKER, timeout=_SELF_TERMINATE_TIMEOUT)
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


def test_wrapped_background_daemon_registers_and_stops(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A real argv wrapper can launch, register, and stop the real daemon child."""
    home = tmp_path / "home"
    marker = tmp_path / "wrapper.json"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import signal",
                "import subprocess",
                "import sys",
                "from pathlib import Path",
                "",
                "child = subprocess.Popen(sys.argv[1:])",
                "",
                "def forward(signum, _frame):",
                "    if child.poll() is None:",
                "        child.send_signal(signum)",
                "",
                "signal.signal(signal.SIGTERM, forward)",
                "signal.signal(signal.SIGINT, forward)",
                "Path(os.environ['OMNIGENT_TEST_WRAPPER_MARKER']).write_text(",
                "    json.dumps({'wrapper_pid': os.getpid(), "
                "'child_pid': child.pid, 'child_argv': sys.argv[1:]})",
                ")",
                "raise SystemExit(child.wait())",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = _connect_env(mock_credentials_env, home)
    env[HOST_DAEMON_COMMAND_ENV_VAR] = json.dumps([str(omnigent_python), str(wrapper)])
    env["OMNIGENT_TEST_WRAPPER_MARKER"] = str(marker)

    proc = _spawn_background_daemon(omnigent_python, omnigent_repo_root, env)
    assert proc.returncode == 0, f"wrapped background spawn failed:\n{proc.stderr}"

    daemon_pid = -1
    wrapper_pid = -1
    server_pid = -1
    record: Path | None = None
    try:
        record = _wait_for_daemon_record(home / ".omnigent" / "daemons", timeout=_BOOT_TIMEOUT)
        payload = json.loads(record.read_text())
        daemon_pid = payload["pid"]
        wrapper_payload = json.loads(marker.read_text())
        wrapper_pid = wrapper_payload["wrapper_pid"]

        assert wrapper_payload["child_pid"] == daemon_pid
        assert wrapper_payload["child_pid"] != wrapper_pid
        assert wrapper_payload["child_argv"] == [
            str(omnigent_python),
            "-P",
            "-m",
            "omnigent.host._daemon_entry",
            "--local",
        ]
        assert payload["mode"] == "local"
        assert payload["target"] == "local"
        assert isinstance(payload["host_id"], str) and payload["host_id"]
        assert Path(payload["log_path"]).is_file()
        assert _pid_alive(wrapper_pid)
        assert _pid_alive(daemon_pid)
        _assert_daemon_owns_record(record, daemon_pid)

        server_pid, _port = _read_local_server_record(home)
        stop = subprocess.run(
            [str(omnigent_python), "-m", "omnigent", "host", "stop"],
            env=dict(env),
            cwd=str(omnigent_repo_root),
            capture_output=True,
            text=True,
            timeout=_BOOT_TIMEOUT,
        )
        assert stop.returncode == 0, f"wrapped host stop failed:\n{stop.stdout}\n{stop.stderr}"
        assert _wait_pid_gone(daemon_pid, timeout=_SELF_TERMINATE_TIMEOUT)
        assert _wait_pid_gone(wrapper_pid, timeout=_SELF_TERMINATE_TIMEOUT)
        assert record is not None and not record.exists()
    finally:
        for pid in (daemon_pid, wrapper_pid, server_pid):
            if pid > 0 and _pid_alive(pid):
                _force_stop_server(pid)


@pytest.mark.skipif(os.name == "nt", reason="wrapped daemon cleanup requires POSIX process groups")
def test_duplicate_wrapped_background_launch_has_one_owner(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Two wrapped launches elect one child and retire the losing supervisor."""
    home = tmp_path / "home"
    marker_dir = tmp_path / "wrapper-markers"
    marker_dir.mkdir(parents=True)
    start = tmp_path / "release-wrappers"
    wrapper = tmp_path / "gated_wrapper.py"
    wrapper.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                "marker_dir = Path(os.environ['OMNIGENT_TEST_DUPLICATE_MARKERS'])",
                "wrapper_pid = os.getpid()",
                "(marker_dir / f'{wrapper_pid}.ready').write_text('ready')",
                "start = Path(os.environ['OMNIGENT_TEST_DUPLICATE_START'])",
                "while not start.exists():",
                "    time.sleep(0.01)",
                "child = subprocess.Popen(sys.argv[1:])",
                "(marker_dir / f'{wrapper_pid}.json').write_text(json.dumps({",
                "    'wrapper_pid': wrapper_pid,",
                "    'child_pid': child.pid,",
                "    'launch_id': os.environ.get('OMNIGENT_HOST_DAEMON_LAUNCH_ID'),",
                "}))",
                "raise SystemExit(child.wait())",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = _connect_env(mock_credentials_env, home)
    env["OMNIGENT_CONFIG_HOME"] = str(home / "config")
    env[HOST_DAEMON_COMMAND_ENV_VAR] = json.dumps([str(omnigent_python), str(wrapper)])
    env["OMNIGENT_TEST_DUPLICATE_MARKERS"] = str(marker_dir)
    env["OMNIGENT_TEST_DUPLICATE_START"] = str(start)

    launchers: list[subprocess.Popen[str]] = []
    wrapper_payloads: list[dict[str, object]] = []
    record: Path | None = None
    server_pid = -1
    try:
        for _ in range(2):
            launchers.append(
                subprocess.Popen(
                    [str(omnigent_python), "-m", "omnigent", "host", "--background", ""],
                    env=dict(env),
                    cwd=str(omnigent_repo_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        deadline = time.monotonic() + _BOOT_TIMEOUT
        while len(list(marker_dir.glob("*.ready"))) < 2 and time.monotonic() < deadline:
            _POLL_PAUSE.wait(0.05)
        assert len(list(marker_dir.glob("*.ready"))) == 2, "both wrappers did not reach the gate"
        start.write_text("go")

        for launcher in launchers:
            stdout, stderr = launcher.communicate(timeout=_BOOT_TIMEOUT)
            assert launcher.returncode == 0, f"duplicate launch failed:\n{stdout}\n{stderr}"

        wrapper_payloads = [json.loads(path.read_text()) for path in marker_dir.glob("*.json")]
        assert len(wrapper_payloads) == 2
        record = _wait_for_daemon_record(home / ".omnigent" / "daemons", timeout=_BOOT_TIMEOUT)
        payload = json.loads(record.read_text())
        child_pids = {entry["child_pid"] for entry in wrapper_payloads}
        assert payload["pid"] in child_pids
        assert payload["launch_id"] in {entry["launch_id"] for entry in wrapper_payloads}

        winner = next(entry for entry in wrapper_payloads if entry["child_pid"] == payload["pid"])
        loser = next(entry for entry in wrapper_payloads if entry["child_pid"] != payload["pid"])
        assert _pid_alive(winner["wrapper_pid"])
        assert _wait_pid_gone(loser["wrapper_pid"], timeout=_SELF_TERMINATE_TIMEOUT)

        server_pid, _port = _read_local_server_record(home)
        stop = subprocess.run(
            [str(omnigent_python), "-m", "omnigent", "host", "stop"],
            env=dict(env),
            cwd=str(omnigent_repo_root),
            capture_output=True,
            text=True,
            timeout=_BOOT_TIMEOUT,
        )
        assert stop.returncode == 0, f"duplicate launch stop failed:\n{stop.stdout}\n{stop.stderr}"
        assert _wait_pid_gone(winner["wrapper_pid"], timeout=_SELF_TERMINATE_TIMEOUT)
        assert record is not None and not record.exists()
    finally:
        for launcher in launchers:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()
        for entry in wrapper_payloads:
            for key in ("wrapper_pid", "child_pid"):
                pid = entry.get(key)
                if isinstance(pid, int) and _pid_alive(pid):
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
        if server_pid > 0 and _pid_alive(server_pid):
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
