"""End-to-end tests for canonical local-server lifecycle.

A foreground ``omnigent server`` registers itself in the machine-global
pidfile AND must stamp the config-signature sidecar, so a later
``omnigent host`` / ``omnigent run`` reuses it instead of stopping
and respawning it. These tests spawn the REAL CLI subprocesses and assert
process survival across the three scenarios the bug report describes:

1. ``server`` then ``connect``      → the foreground server survives.
2. ``connect`` then ``server``      → the connect-owned server survives
   (the second ``server`` reuses it and exits without binding).
3. ``server`` + ``server --port X`` → an explicit ``--port`` is a
   dedicated server; both run side by side, neither is torn down.

No LLM is needed — this is pure process-lifecycle wiring — so these run
without ``--llm-api-key``::

    .venv/bin/python -m pytest tests/e2e/test_local_server_lifecycle_e2e.py -v

Each test isolates ``$HOME`` to a tmp dir so the pidfile / sig / DB land
under ``<home>/.omnigent`` and never touch the developer's real
``~/.omnigent`` or a server on the real :8000 (a busy :8000 just makes
the canonical server fall back to a free port, recorded in the isolated
pidfile — discovery is via the pidfile, never the port).
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Server boot budget — shared with the e2e suite's live_server fixture: a
# cold `omnigent server` imports the whole stack and runs SQLite
# migrations before /health answers.
_SERVER_BOOT_TIMEOUT_S = HEALTH_TIMEOUT_S
_POLL_INTERVAL_S = POLL_INTERVAL_S
# How long to let `connect` settle after reuse before we trust the verdict.
# The bug (stop + respawn) fires within ~1-2s of connect calling
# ensure_local_omnigent_server, so a host appearing online on the ORIGINAL port
# within this window is decisive.
_HOST_ONLINE_TIMEOUT_S = 45.0
# Env vars that would leak the coding-agent harness's own creds / config
# into the server under test (CLAUDE.md hygiene) or break HOME isolation.
_ENV_TO_CLEAR = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE",
    "CODEX",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_AUTH_ENABLED",
    # An ambient issuer would select oidc once auth is (accidentally) enabled.
    "OMNIGENT_OIDC_ISSUER",
    "OMNIGENT_AUTH_PROVIDER",
    # ensure_local_omnigent_server honors OMNIGENT_DATABASE_URI over the isolated
    # tmp sqlite db, and the server honors OMNIGENT_RUNNER_TUNNEL_TOKEN — if
    # either is set on the dev box / CI, the spawned server escapes the
    # isolated HOME (shared DB, tunnel-token allowlist) and the test flakes.
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
)


def _pid_alive(pid: int) -> bool:
    """Return whether a process id is currently alive.

    :param pid: Process id to probe, e.g. ``12345``.
    :returns: ``True`` if the process exists, ``False`` once it has exited.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_pid(pid: int, timeout: float) -> None:
    """Wait for a (detached, non-child) pid to die, SIGKILL if it doesn't.

    The detached connect-owned server isn't our child, so ``waitpid`` won't
    work — poll liveness instead, then escalate to SIGKILL.

    :param pid: Process id already sent SIGTERM, e.g. ``12345``.
    :param timeout: Seconds to wait for a clean exit before SIGKILL.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(_POLL_INTERVAL_S)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def _isolated_env(home: Path) -> dict[str, str]:
    """Build a subprocess env with an isolated ``$HOME`` and no leaked creds.

    Header-auth (the default) single-user loopback mode: no accounts, no
    Databricks profile. PYTHONPATH pins the worktree checkout so the
    subprocess imports the branch under test, not a stale installed wheel.

    :param home: The tmp home dir; ``<home>/.omnigent`` holds the pidfile,
        sig, DB, and artifacts for this test.
    :returns: The environment dict for ``subprocess.Popen``.
    """
    env = dict(os.environ)
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return env


def _pidfile_path(home: Path) -> Path:
    """Return the canonical local-server pidfile path under an isolated home.

    :param home: The isolated home dir.
    :returns: ``<home>/.omnigent/local_server.pid``.
    """
    return home / ".omnigent" / "local_server.pid"


def _read_pidfile(path: Path) -> tuple[int, int] | None:
    """Read ``<pid>\\n<port>\\n`` from the pidfile.

    :param path: Pidfile path.
    :returns: ``(pid, port)`` when well-formed, else ``None``.
    """
    try:
        lines = path.read_text().strip().splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        return int(lines[0]), int(lines[1])
    except ValueError:
        return None


def _health_ok(port: int) -> bool:
    """Return whether ``/health`` answers 200 on a loopback port.

    :param port: Loopback TCP port, e.g. ``8000``.
    :returns: ``True`` on HTTP 200, else ``False``.
    """
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def _find_free_port() -> int:
    """Find a free loopback TCP port.

    :returns: An available port number on ``127.0.0.1``.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_pidfile_server(
    home: Path, proc: subprocess.Popen[bytes], log: Path
) -> tuple[int, int]:
    """Wait until the canonical server records itself and answers /health.

    :param home: Isolated home holding the pidfile.
    :param proc: The server subprocess, polled so a crashed boot fails fast.
    :param log: Captured stdout/stderr, surfaced on timeout.
    :returns: ``(pid, port)`` of the healthy server from the pidfile.
    :raises AssertionError: If the server never records a healthy entry.
    """
    pidfile = _pidfile_path(home)
    deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited early (rc={proc.returncode}).\n--- log ---\n{_tail(log)}"
            )
        entry = _read_pidfile(pidfile)
        if entry is not None and _pid_alive(entry[0]) and _health_ok(entry[1]):
            return entry
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"canonical server never became healthy within {_SERVER_BOOT_TIMEOUT_S}s.\n"
        f"--- log ---\n{_tail(log)}"
    )


def _wait_for_health(port: int, proc: subprocess.Popen[bytes], log: Path) -> None:
    """Wait until an explicit-port server answers /health.

    :param port: The port the server was told to bind.
    :param proc: The server subprocess, polled so a crashed boot fails fast.
    :param log: Captured stdout/stderr, surfaced on timeout.
    :raises AssertionError: If the server never answers.
    """
    deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"server on :{port} exited early (rc={proc.returncode}).\n"
                f"--- log ---\n{_tail(log)}"
            )
        if _health_ok(port):
            return
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"server on :{port} never answered /health within {_SERVER_BOOT_TIMEOUT_S}s.\n"
        f"--- log ---\n{_tail(log)}"
    )


def _wait_for_host_online(port: int, timeout: float = _HOST_ONLINE_TIMEOUT_S) -> None:
    """Poll ``GET /v1/hosts`` on a port until a host reports online.

    Decisive proof that ``connect`` reached ``run_host_process`` and bound
    its tunnel to THIS server. If connect had instead stopped + respawned
    (the stop-and-respawn bug), this port's server would be dead and the
    request would raise ``ConnectError`` until timeout.

    :param port: The original server's port to query.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If no host appears online in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/v1/hosts", timeout=2.0)
            if resp.status_code == 200:
                hosts = resp.json().get("hosts", [])
                if any(h.get("status") == "online" for h in hosts):
                    return
        except httpx.HTTPError:
            pass
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"no host came online on :{port} within {timeout}s — connect did not "
        f"reuse the running server (it crashed or respawned a replacement)."
    )


def _respawned_server_pids(home: Path) -> set[int]:
    """Return PIDs of detached local servers spawned against this test's HOME.

    The single-server invariant for scenario 1: ``connect`` must REUSE the
    foreground server, not spawn a competitor. Any respawn goes through
    :func:`ensure_local_omnigent_server`, which spawns a detached
    ``omnigent server --database-uri sqlite:///<home>/.omnigent/chat.db
    --artifact-location <home>/.omnigent/artifacts`` — so its argv carries
    this isolated HOME path. The reused foreground server (spawned here as a
    bare ``["server"]``) does NOT, so every match is a respawned competitor,
    never the original. This is independent of the pidfile, so it catches a
    respawn that preserved the pidfile (e.g. a stray bound to a random port).

    :param home: The test's isolated home dir.
    :returns: PIDs of live ``omnigent.cli ... server`` processes whose
        command line references ``home``; empty when ``connect`` reused.
    """
    # ``ww`` disables ps's column-width truncation: the home path sits late in
    # the argv (after ``--database-uri``), so a truncated line would hide a
    # stray respawn (a false-negative pass). A captured pipe already avoids
    # tty-width truncation, but ``ww`` makes it unconditional on Linux too.
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    # Match the home as a directory PREFIX (``<home>/.omnigent/...`` always
    # appears in a respawn's argv), not a bare substring — so a sibling home
    # sharing a prefix (``/tmp/x/home`` vs ``/tmp/x/home2``) can't false-match.
    home_prefix = f"{home}{os.sep}"
    pids: set[int] = set()
    for line in out.splitlines():
        if "omnigent.cli" not in line or home_prefix not in line:
            continue
        if not re.search(r"\bserver\b", line):
            continue
        with contextlib.suppress(ValueError, IndexError):
            pids.add(int(line.split(maxsplit=1)[0]))
    return pids


def _tail(log: Path, n: int = 40) -> str:
    """Return the last ``n`` lines of a log file for failure diagnostics.

    :param log: Log file path.
    :param n: Number of trailing lines.
    :returns: The tail text, or a placeholder if unreadable/empty.
    """
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(could not read {log}: {exc})"
    return "\n".join(lines[-n:]) if lines else "(empty log)"


class _Procs:
    """Tracks spawned CLI subprocesses and tears them all down.

    Also reaps any detached server recorded in the pidfile: ``connect``
    spawns the local server with ``start_new_session=True``, so SIGTERM to
    ``connect`` does not cascade to it.
    """

    def __init__(self) -> None:
        self._procs: list[subprocess.Popen[bytes]] = []
        self._logs: list[object] = []
        self._pidfiles: list[Path] = []
        self._homes: list[Path] = []

    def spawn(
        self, args: list[str], *, env: dict[str, str], cwd: Path, log: Path
    ) -> subprocess.Popen[bytes]:
        """Spawn a CLI subprocess with output captured to ``log``.

        :param args: CLI args after the ``python -m omnigent.cli`` prefix.
        :param env: Subprocess environment.
        :param cwd: Working directory (an isolated home).
        :param log: File to capture combined stdout/stderr.
        :returns: The process handle.
        """
        # Held open for the subprocess's lifetime; closed in teardown.
        fh = open(log, "wb")  # noqa: SIM115
        self._logs.append(fh)
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.cli", *args],
            env=env,
            cwd=str(cwd),
            stdout=fh,
            stderr=fh,
        )
        self._procs.append(proc)
        return proc

    def track_pidfile(self, path: Path) -> None:
        """Reap the detached server recorded here on teardown.

        :param path: A canonical-server pidfile to drain at teardown.
        """
        self._pidfiles.append(path)

    def track_server_home(self, home: Path) -> None:
        """Reap any detached server respawned against ``home`` on teardown.

        The single-server assertion should fail (not leak) a stray respawn,
        but on assertion failure the stray would survive teardown otherwise:
        it's detached (``start_new_session``) and absent from the pidfile, so
        neither the tracked-process nor pidfile reaping reaches it. Scanning
        for servers carrying ``home`` in their argv closes that leak.

        :param home: An isolated home whose respawned servers to reap.
        """
        self._homes.append(home)

    def teardown(self) -> None:
        """SIGTERM every tracked process and detached server, then SIGKILL.

        ``connect`` spawns the local server in a new session, so SIGTERM to
        ``connect`` does not cascade to it — we must signal it via the
        pidfile AND escalate to SIGKILL if it ignores SIGTERM, or it leaks
        across tests / xdist workers.
        """
        detached_pids = [
            entry[0]
            for path in self._pidfiles
            if (entry := _read_pidfile(path)) is not None and _pid_alive(entry[0])
        ]
        for pid in detached_pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        for proc in self._procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in self._procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # Reap detached servers: wait briefly for a clean exit, SIGKILL any
        # that ignored SIGTERM so none leak past the test.
        for pid in detached_pids:
            _reap_pid(pid, timeout=5.0)
        # Reap any server respawned against a tracked home (the single-server
        # assertion's failure mode): not a child, not in the pidfile, so only
        # the argv scan finds it. SIGKILL directly — there's no clean-exit
        # contract for a server the test never meant to spawn.
        for home in self._homes:
            for pid in _respawned_server_pids(home):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        for fh in self._logs:
            with contextlib.suppress(OSError):
                fh.close()  # type: ignore[attr-defined]


@pytest.fixture
def procs() -> Iterator[_Procs]:
    """Process registry that tears everything down after the test.

    :returns: An :class:`_Procs` registry.
    """
    registry = _Procs()
    try:
        yield registry
    finally:
        registry.teardown()


def test_connect_reuses_foreground_server_without_killing_it(
    procs: _Procs,
    tmp_path: Path,
) -> None:
    """Scenario 1: ``server`` then ``connect`` — the server survives.

    Before the fix, the foreground server stamped no config-sig sidecar, so
    connect saw ``None != desired`` and SIGTERM'd it. We assert the original
    PID stays alive, the pidfile is unchanged, and a host comes online on the
    ORIGINAL port — proving connect bound to the SAME server, not a respawn.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    procs.track_pidfile(_pidfile_path(home))
    procs.track_server_home(home)

    server = procs.spawn(["server"], env=env, cwd=home, log=tmp_path / "server.log")
    pid1, port1 = _wait_for_pidfile_server(home, server, tmp_path / "server.log")

    connect = procs.spawn(["host"], env=env, cwd=home, log=tmp_path / "connect.log")

    # Decisive: connect must connect to the original server (port1). A
    # respawn would have killed port1, so this would time out.
    _wait_for_host_online(port1)

    assert connect.poll() is None, (
        f"connect exited unexpectedly (rc={connect.returncode}).\n"
        f"--- connect log ---\n{_tail(tmp_path / 'connect.log')}"
    )
    # The original foreground server was reused, not torn down + respawned.
    assert _pid_alive(pid1), "foreground server was killed by connect"
    assert _read_pidfile(_pidfile_path(home)) == (pid1, port1), (
        "pidfile changed — connect respawned a replacement server"
    )
    assert _health_ok(port1), "original server stopped answering /health"
    # Direct single-server invariant: the asserts above prove the
    # ORIGINAL survived but, on their own, only catch a respawn TRANSITIVELY —
    # via the pidfile rewrite that `ensure_local_omnigent_server` happens to do. A
    # respawn that left the pidfile pointing at the original (a stray bound to
    # a random port) would slip past them. Assert directly that connect spawned
    # no detached server for this isolated HOME, so exactly one server exists.
    strays = _respawned_server_pids(home)
    assert strays == set(), (
        f"connect spawned {len(strays)} detached local server(s) {sorted(strays)} "
        f"instead of reusing the foreground one — exactly one server must exist.\n"
        f"--- connect log ---\n{_tail(tmp_path / 'connect.log')}"
    )


def test_foreground_server_reuses_connect_owned_server(
    procs: _Procs,
    tmp_path: Path,
) -> None:
    """Scenario 2 (vice versa): ``connect`` then ``server`` — both survive.

    The connect-owned local server is already healthy in the pidfile, so a
    second canonical ``omnigent server`` must reuse it (print "already
    running — reusing it" and exit 0) rather than bind a competing server or
    tear the running one down.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    procs.track_pidfile(_pidfile_path(home))

    connect = procs.spawn(["host"], env=env, cwd=home, log=tmp_path / "connect.log")
    # connect spawns the detached local server and records it; wait for it.
    pid_srv, port = _wait_for_pidfile_server(home, connect, tmp_path / "connect.log")
    _wait_for_host_online(port)

    server2 = procs.spawn(["server"], env=env, cwd=home, log=tmp_path / "server2.log")
    rc = server2.wait(timeout=_SERVER_BOOT_TIMEOUT_S)

    assert rc == 0, (
        f"second `omnigent server` exited non-zero (rc={rc}).\n"
        f"--- log ---\n{_tail(tmp_path / 'server2.log')}"
    )
    assert "reusing it" in _tail(tmp_path / "server2.log"), (
        "second server did not report reusing the running one"
    )
    # The connect-owned server was untouched.
    assert _pid_alive(pid_srv), "the running server was killed by a second `server`"
    assert _read_pidfile(_pidfile_path(home)) == (pid_srv, port)
    assert _health_ok(port)


def test_explicit_port_servers_run_side_by_side(
    procs: _Procs,
    tmp_path: Path,
) -> None:
    """Scenario 3: two ``server --port`` instances coexist, neither killed.

    An explicit ``--port`` marks a DEDICATED server: it binds the exact port
    and must NOT consult or register in the shared pidfile. Two such servers
    on different ports run independently — the canonical reuse/respawn logic
    never engages.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    # Two distinct free ports: the probe socket closes immediately, so the OS
    # could hand out the same ephemeral port twice — loop until they differ.
    port_a = _find_free_port()
    port_b = _find_free_port()
    while port_b == port_a:
        port_b = _find_free_port()

    srv_a = procs.spawn(
        ["server", "--port", str(port_a)], env=env, cwd=home, log=tmp_path / "a.log"
    )
    _wait_for_health(port_a, srv_a, tmp_path / "a.log")

    srv_b = procs.spawn(
        ["server", "--port", str(port_b)], env=env, cwd=home, log=tmp_path / "b.log"
    )
    _wait_for_health(port_b, srv_b, tmp_path / "b.log")

    # Both alive, both serving — neither tore the other down.
    assert srv_a.poll() is None and srv_b.poll() is None
    assert _health_ok(port_a) and _health_ok(port_b)
    # A dedicated (explicit-port) server never registers in the shared pidfile.
    assert not _pidfile_path(home).exists(), (
        "an explicit-port server wrote the canonical pidfile — it must stay dedicated"
    )
