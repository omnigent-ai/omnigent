"""E2E coverage for the ``omnigent host`` Ctrl+C stop-server prompt.

``omnigent host ""`` (local mode) spawns a *detached* background local
AP server (:func:`omnigent.host.local_server.ensure_local_omnigent_server`) that
intentionally outlives the foreground host daemon so sessions and the Web
UI stay reachable across ``host`` / ``run``. Because users expect Ctrl+C
to stop "everything", the connect command now prompts on a clean stop:

    Stop it too? [y/N]

These tests drive the real CLI under a PTY, send a real SIGINT (Ctrl+C),
and verify the branches against the *actual* server process:

1. Answering ``y`` stops the detached server — its ``/health`` endpoint
   stops responding.
2. Answering ``n`` leaves the server running — ``/health`` still answers
   200 after the host process has exited. The test then stops the
   stranded server itself so it does not leak past the test.
3. When connect *reuses* a server it did not spawn (one already brought up
   by a prior daemon, via the real ``ensure_local_omnigent_server`` path), Ctrl+C
   shows NO prompt and leaves that server running — connect must never
   offer to stop a server it didn't start.

The detached server is the genuine production object (a real ``omnigent
server`` subprocess), so these tests fail loudly if the prompt is dropped,
wired to the wrong default, fires for a reused server, or if ``y`` fails to
actually terminate the server — none of which a mock-based test would catch.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

import httpx
import pexpect

# The host daemon's WS tunnel + local-server boot take the same path the
# REPL lifecycle e2e exercises; 90s mirrors that suite's readiness budget so
# a cold import + server start on a loaded CI box still settles.
_BOOT_TIMEOUT = 90.0
_PROMPT_TIMEOUT = 30.0
_EXIT_TIMEOUT = 30.0
# Bounded poll budget for observing the detached server flip up/down. The
# server lives in another process, so we poll its /health rather than wait on
# an in-process signal.
_HEALTH_POLL_TIMEOUT = 30.0

# ``run_host_process`` prints this once the host tunnel is up and the local
# server is confirmed reachable — our readiness signal that Ctrl+C will now
# land inside the asyncio run loop (not mid-server-spawn).
_LISTENING_MARKER = "Listening for sessions"
# The ``click.confirm`` prompt text emitted on a clean stop in local mode.
_PROMPT_MARKER = "Stop it too?"
_STOPPED_MARKER = "Stopped the local server"
_LEFT_RUNNING_MARKER = "Left the local server running"

# Shared, interruptible sleep handle for bounded external-state polling (no
# raw time.sleep — see omnigent-testing rule 13).
_POLL_PAUSE = threading.Event()


def _connect_env(base_env: Mapping[str, str], home: Path) -> dict[str, str]:
    """
    Build the subprocess environment for a ``host`` PTY run.

    Isolates ``HOME`` so the local-server pidfile, host registry, and sqlite
    db land under the per-test directory (``ensure_local_omnigent_server`` keys its
    data dir off ``~/.omnigent`` when ``OMNIGENT_DATA_DIR`` is unset),
    keeping the test from touching the developer's real local server.

    :param base_env: Fixture-provided credentials environment, e.g.
        ``mock_credentials_env``.
    :param home: Isolated HOME for this test's runtime data.
    :returns: Environment dict for ``pexpect.spawn``.
    """
    env = dict(base_env)
    env["HOME"] = str(home)
    env["TERM"] = "xterm-256color"
    env["LINES"] = "40"
    env["COLUMNS"] = "120"
    return env


def _spawn_connect(
    omnigent_python: Path,
    repo_root: Path,
    env: Mapping[str, str],
) -> pexpect.spawn:
    """
    Spawn ``omnigent host ""`` (local mode) under a real PTY.

    The empty positional argument selects local mode — connect spawns (or
    reuses) the detached local Omnigent server and connects the foreground daemon
    to it. Databricks auth comes from the env (the ``--profile`` flag was
    removed from the omnigent CLI).

    :param omnigent_python: Python interpreter with Omnigent installed.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Subprocess environment from :func:`_connect_env`.
    :returns: A live pexpect child.
    """
    return pexpect.spawn(
        str(omnigent_python),
        ["-m", "omnigent", "host", ""],
        env=dict(env),
        cwd=str(repo_root),
        encoding="utf-8",
        codec_errors="replace",
        timeout=_BOOT_TIMEOUT,
        dimensions=(40, 120),
    )


def _read_local_server_record(home: Path) -> tuple[int, int]:
    """
    Read the detached local server's pid + port from the pidfile.

    :param home: Isolated HOME passed to the connect subprocess.
    :returns: ``(pid, port)`` recorded by ``ensure_local_omnigent_server``.
    :raises AssertionError: If the pidfile is missing or malformed.
    """
    pid_path = home / ".omnigent" / "local_server.pid"
    try:
        lines = pid_path.read_text().strip().splitlines()
        return int(lines[0]), int(lines[1])
    except (IndexError, OSError, ValueError) as exc:
        raise AssertionError(f"missing or malformed local server pidfile at {pid_path}") from exc


def _server_healthy(port: int) -> bool:
    """
    Return whether the local server answers ``/health`` with 200.

    :param port: Loopback port the server bound, e.g. ``8000``.
    :returns: ``True`` when ``/health`` responds 200, ``False`` on any
        non-200 or transport error (connection refused → server down).
    """
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def _wait_for_health(port: int, *, expected: bool, timeout: float) -> bool:
    """
    Poll ``/health`` until it reaches the expected up/down state.

    :param port: Loopback port the server bound.
    :param expected: Target reachability — ``True`` waits for the server to
        be up, ``False`` waits for it to go down.
    :param timeout: Max seconds to poll.
    :returns: ``True`` if the expected state was observed within *timeout*,
        else ``False``.
    """
    elapsed = 0.0
    while elapsed < timeout:
        if _server_healthy(port) == expected:
            return True
        # Bounded pause between external-process probes (rule 13).
        _POLL_PAUSE.wait(0.25)
        elapsed += 0.25
    return _server_healthy(port) == expected


def _force_stop_server(pid: int) -> None:
    """
    Best-effort SIGTERM the detached local server so it never leaks.

    :param pid: Recorded server process id.
    :returns: None.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)


def _boot_connect_and_get_server(child: pexpect.spawn, home: Path) -> tuple[int, int]:
    """
    Wait for connect to be fully up and return the live server's pid/port.

    Blocks on the ``Listening for sessions`` banner (proves the tunnel is up
    and we are inside the asyncio run loop, so a subsequent SIGINT lands on
    the clean-stop path), then confirms the detached server actually answers
    ``/health`` before the test proceeds.

    :param child: Live connect pexpect child.
    :param home: Isolated HOME holding the server pidfile.
    :returns: ``(pid, port)`` of the running detached server.
    """
    child.expect(_LISTENING_MARKER, timeout=_BOOT_TIMEOUT)
    pid, port = _read_local_server_record(home)
    assert _wait_for_health(port, expected=True, timeout=_HEALTH_POLL_TIMEOUT), (
        f"detached local server on port {port} never became healthy after the "
        f"host daemon reported it was listening"
    )
    return pid, port


def _prespawn_persistent_server(
    omnigent_python: Path,
    repo_root: Path,
    env: Mapping[str, str],
) -> tuple[int, int]:
    """
    Bring up the persistent local server the way a prior daemon would.

    Invokes the real :func:`ensure_local_omnigent_server` in a short-lived
    subprocess so it spawns the detached server AND stamps the config-
    signature sidecar with the same auth config a later ``host`` will
    compute (both subprocesses share the same env, so the signatures
    agree). That matching signature is what makes connect *reuse* this
    server (``spawned=False``) instead of treating it as config drift and
    respawning — i.e. it reproduces "a server is already running that connect
    did not start".

    :param omnigent_python: Python interpreter with Omnigent installed.
    :param repo_root: Checkout root used as the subprocess cwd.
    :param env: Subprocess environment (isolated HOME) from
        :func:`_connect_env`.
    :returns: ``(pid, port)`` of the running detached server.
    """
    code = (
        "from omnigent.host.local_server import ensure_local_omnigent_server;"
        "print(ensure_local_omnigent_server().url)"
    )
    proc = subprocess.run(
        [str(omnigent_python), "-c", code],
        env=dict(env),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_BOOT_TIMEOUT,
    )
    home = Path(env["HOME"])
    # The subprocess detaches the server (start_new_session=True) before
    # returning, so any failure past the spawn would otherwise leak a live
    # server into later tests. Stop it via the pidfile before re-raising.
    try:
        assert proc.returncode == 0, f"pre-spawn failed (rc={proc.returncode}):\n{proc.stderr}"
        pid, port = _read_local_server_record(home)
        assert _wait_for_health(port, expected=True, timeout=_HEALTH_POLL_TIMEOUT), (
            f"pre-spawned local server on port {port} never became healthy"
        )
        return pid, port
    except BaseException:
        with contextlib.suppress(AssertionError, OSError, ValueError, IndexError):
            leaked_pid, _leaked_port = _read_local_server_record(home)
            _force_stop_server(leaked_pid)
        raise


def test_host_ctrl_c_yes_stops_local_server(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Ctrl+C then ``y`` stops the detached local server.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)
    child = _spawn_connect(omnigent_python, omnigent_repo_root, env)
    server_pid = -1
    try:
        server_pid, port = _boot_connect_and_get_server(child, home)

        # Real SIGINT to the foreground host process group. The detached
        # server runs in its own session (start_new_session=True) so it does
        # NOT receive this signal — only the prompt decides its fate.
        child.sendcontrol("c")
        child.expect(_PROMPT_MARKER, timeout=_PROMPT_TIMEOUT)
        child.send("y\r")

        # The prompt's success line proves stop_local_omnigent_server() was invoked.
        child.expect(_STOPPED_MARKER, timeout=_PROMPT_TIMEOUT)
        child.expect(pexpect.EOF, timeout=_EXIT_TIMEOUT)

        # The decisive end-to-end assertion: the real server process actually
        # went down. If the prompt's "yes" branch were wired wrong (or stop
        # was a no-op), /health would keep answering 200 here.
        assert _wait_for_health(port, expected=False, timeout=_HEALTH_POLL_TIMEOUT), (
            f"local server on port {port} was still healthy after answering "
            f"'y' — the stop-server prompt did not actually stop it"
        )
    finally:
        if server_pid > 0:
            _force_stop_server(server_pid)
        if not child.closed:
            child.close(force=True)


def test_host_ctrl_c_no_leaves_local_server_running(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Ctrl+C then ``n`` leaves the detached local server running.

    The test stops the surviving server itself in teardown so it does not
    leak past the test (mirroring how a user would reuse it across
    ``host`` / ``run`` and stop it later).

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)
    child = _spawn_connect(omnigent_python, omnigent_repo_root, env)
    server_pid = -1
    try:
        server_pid, port = _boot_connect_and_get_server(child, home)

        child.sendcontrol("c")
        child.expect(_PROMPT_MARKER, timeout=_PROMPT_TIMEOUT)
        child.send("n\r")

        # The decline line proves we took the "leave it running" branch.
        child.expect(_LEFT_RUNNING_MARKER, timeout=_PROMPT_TIMEOUT)
        child.expect(pexpect.EOF, timeout=_EXIT_TIMEOUT)

        # The decisive end-to-end assertion: the real server is STILL up after
        # the host process exited. If "no" accidentally stopped it (or the
        # default were inverted), /health would fail here.
        assert _server_healthy(port), (
            f"local server on port {port} was stopped after answering 'n' — "
            f"declining the prompt must leave the detached server running"
        )
    finally:
        # The whole point of "no" is that the server survives the connect
        # process, so the test owns stopping it to avoid leaking a server.
        if server_pid > 0:
            _force_stop_server(server_pid)
        if not child.closed:
            child.close(force=True)


def test_host_ctrl_c_reused_server_shows_no_prompt(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Ctrl+C shows NO prompt when connect reused a server it did not spawn.

    A server is brought up first via the real ``ensure_local_omnigent_server`` path
    (as a prior ``run`` / ``host`` daemon would), then ``connect ""``
    reuses it. On Ctrl+C there must be no stop-server prompt — connect must
    never offer to stop a server it didn't start — and the server must still
    be running after connect exits.

    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture (subprocess cwd).
    :param mock_credentials_env: Mock-LLM credential environment fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)

    # Bring the server up first, independently of connect, with a config
    # signature that matches what connect will compute — so connect reuses it.
    server_pid, port = _prespawn_persistent_server(omnigent_python, omnigent_repo_root, env)
    child: pexpect.spawn | None = None
    try:
        child = _spawn_connect(omnigent_python, omnigent_repo_root, env)
        # Connect attaches to the already-running server (same pid/port — it
        # reused it rather than spawning a new one).
        child.expect(_LISTENING_MARKER, timeout=_BOOT_TIMEOUT)
        reused_pid, reused_port = _read_local_server_record(home)
        assert (reused_pid, reused_port) == (server_pid, port), (
            f"connect did not reuse the pre-spawned server: expected "
            f"pid/port {(server_pid, port)}, pidfile now shows "
            f"{(reused_pid, reused_port)}"
        )

        child.sendcontrol("c")
        # Connect must exit cleanly with NO prompt. If the prompt fired,
        # ``click.confirm`` would block on stdin (we send nothing), so we'd
        # match the prompt marker instead of EOF — caught explicitly below.
        idx = child.expect([pexpect.EOF, _PROMPT_MARKER], timeout=_EXIT_TIMEOUT)
        assert idx == 0, (
            "connect offered to stop a server it reused (did not spawn) — the "
            "stop-server prompt must only appear for a server connect started"
        )

        # The reused server is untouched and still serving.
        assert _server_healthy(port), (
            f"reused local server on port {port} went down after connect exited "
            f"— connect must not stop a server it did not spawn"
        )
    finally:
        _force_stop_server(server_pid)
        if child is not None and not child.closed:
            child.close(force=True)
