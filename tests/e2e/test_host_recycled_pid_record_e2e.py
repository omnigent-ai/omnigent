"""Recycled-PID daemon record e2e.

A host daemon's registry record (``<data-dir>/daemons/<hash>.json``) names its
owner by bare PID. After a reboot the record survives, its flock is gone, and
the kernel can hand the recorded PID to an unrelated process (on the reporter's
machine, root's ``wifianalyticsd``). The daemon-record liveness check then
treats the recycled PID as a live daemon, with two operator-visible symptoms:

1. ``omnigent host --server <url>`` refuses to start, forever
   (``A host daemon is already running for this server (pid=...)``) — under a
   LaunchAgent/systemd unit this is a silent unattended outage.
2. The documented recovery ``omnigent stop`` reached ``os.kill`` on the
   foreign-owned PID and crashed with ``PermissionError`` (EPERM). This half
   was hardened separately (``_signal_daemon_pid`` treats EPERM as a stale
   record); the test below keeps it guarded end to end at the real CLI.

Both tests drive the REAL CLI journey. The record is written by a real
``omnigent host`` run against a local hanging server; the reboot is simulated
by SIGKILL (process dies without cleanup, kernel drops the flock) and PID
recycling by repointing the record's ``pid`` at a live process that is not an
omnigent daemon, with ``started_at`` backdated (the record always predates the
recycled process). The recycled-PID stand-in is a ``sleep`` child: alive, a
create time far newer than the record's ``started_at``, and a non-omnigent
command line. Ownership is the one axis an unprivileged test cannot vary, so a
correct fix must judge record identity by process start time / command line
(which also covers same-user PID recycling), not merely by ownership.

Usage::

    python -m pytest tests/e2e/test_host_recycled_pid_record_e2e.py -v --timeout=180
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="daemon records rely on flock/SIGKILL semantics that are POSIX-only",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Generous stage budgets: CLI cold start is a few seconds; polls exit early.
_RECORD_WAIT_S = 90.0
_RECLAIM_WAIT_S = 90.0
_STOP_TIMEOUT_S = 120.0
# The record must predate the recycled process by far more than any
# plausible start-time tolerance a fix might use.
_RECORD_BACKDATE_S = 7 * 24 * 3600

_ALREADY_RUNNING = "A host daemon is already running"

# LD_PRELOAD shim: kill(EPERM_PID, sig) fails with EPERM, exactly as the
# kernel answers when the PID belongs to another user. This injects the
# foreign-owner half of PID recycling, which an unprivileged test cannot
# arrange for real (it cannot mint processes under another uid).
_EPERM_SHIM_SRC = """\
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <sys/types.h>

int kill(pid_t pid, int sig) {
    const char *t = getenv("EPERM_PID");
    if (t && pid == (pid_t)atoi(t)) {
        errno = EPERM;
        return -1;
    }
    int (*real_kill)(pid_t, int) = (int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill");
    return real_kill(pid, sig);
}
"""


def _cli_env(state_dir: Path, home_dir: Path) -> dict[str, str]:
    """Build an isolated environment for spawned ``omnigent`` subprocesses.

    :param state_dir: Directory for ``OMNIGENT_DATA_DIR`` (daemon registry).
    :param home_dir: Fake ``HOME`` so real user config/identity stay untouched.
    :returns: Env mapping resolving ``omnigent`` to this worktree.
    """
    (home_dir / ".omnigent").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["OMNIGENT_DATA_DIR"] = str(state_dir)
    env["HOME"] = str(home_dir)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    return env


def _spawn_host(server_url: str, env: dict[str, str]) -> subprocess.Popen[str]:
    """Spawn the real foreground host CLI, exactly as an unattended unit does.

    :param server_url: Server target, e.g. ``"http://127.0.0.1:51913"``.
    :param env: Environment from :func:`_cli_env`.
    :returns: The running ``omnigent host`` process.
    """
    return subprocess.Popen(
        [sys.executable, "-m", "omnigent", "host", "--server", server_url, "--non-interactive"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _kill_and_reap(proc: subprocess.Popen[str]) -> None:
    """SIGKILL *proc* if still alive and reap it.

    :param proc: Process to dispose of.
    """
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)


@pytest.fixture()
def hanging_server() -> Iterator[str]:
    """A loopback listener that accepts connections but never answers.

    Stands in for the remote server: the foreground host gets past its
    record claim and parks in the connect phase instead of exiting (and
    cleaning its record up) immediately.

    :returns: Base URL of the listener, e.g. ``"http://127.0.0.1:51913"``.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


@pytest.fixture()
def recycled_pid() -> Iterator[int]:
    """A live PID that is not an omnigent daemon (the recycled-PID stand-in).

    :returns: PID of a ``sleep`` child that outlives the test body.
    """
    proc = subprocess.Popen(["sleep", "3600"])
    try:
        yield proc.pid
    finally:
        _kill_and_reap(proc)


def _stage_stale_record(
    server_url: str,
    env: dict[str, str],
    state_dir: Path,
    recycled_pid: int,
) -> Path:
    """Produce the post-reboot state: a genuine record pointing at a recycled PID.

    Runs the real ``omnigent host`` so the record is authored by product code,
    SIGKILLs it (reboot: no cleanup, flock dropped by the kernel), then
    repoints the surviving record's ``pid`` at *recycled_pid* and backdates
    ``started_at`` (the record always predates the recycled process).

    :param server_url: Hanging server target the host connects to.
    :param env: Environment from :func:`_cli_env`.
    :param state_dir: The ``OMNIGENT_DATA_DIR`` holding ``daemons/``.
    :param recycled_pid: Live non-omnigent PID to repoint the record at.
    :returns: Path of the staged daemon record.
    """
    daemons = state_dir / "daemons"
    host = _spawn_host(server_url, env)
    record_path: Path | None = None
    try:
        deadline = time.monotonic() + _RECORD_WAIT_S
        while time.monotonic() < deadline:
            found = sorted(daemons.glob("*.json")) if daemons.is_dir() else []
            if found:
                record_path = found[0]
                break
            if host.poll() is not None:
                out = host.stdout.read() if host.stdout else ""
                pytest.fail(f"staging host exited before writing its daemon record:\n{out}")
            time.sleep(0.05)
        if record_path is None:
            pytest.fail("staging host never wrote a daemon record")
        record = json.loads(record_path.read_text())
        assert record["pid"] == host.pid, "record should name the foreground host process"
        # Let the host settle into its connect phase before "rebooting" it.
        time.sleep(0.5)
    finally:
        _kill_and_reap(host)
    assert record_path.exists(), "daemon record should survive the crash"
    record["pid"] = recycled_pid
    record["started_at"] = int(record["started_at"]) - _RECORD_BACKDATE_S
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record_path


def test_host_start_recovers_from_recycled_pid_record(
    tmp_path: Path,
    hanging_server: str,
    recycled_pid: int,
) -> None:
    """``omnigent host`` must start despite a stale record naming a recycled PID.

    Today the liveness check trusts bare PID existence once the record's flock
    is free, so the recycled PID reads as a live daemon and every start is
    refused with "A host daemon is already running" — under LaunchAgent/systemd
    ``KeepAlive`` an unattended, permanent outage. A fixed host treats the
    record as stale (the PID is not the process that wrote it), replaces it,
    and proceeds to connect.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _cli_env(state_dir, tmp_path / "home")
    record_path = _stage_stale_record(hanging_server, env, state_dir, recycled_pid)

    host = _spawn_host(hanging_server, env)
    reclaimed = False
    output = ""
    try:
        deadline = time.monotonic() + _RECLAIM_WAIT_S
        while time.monotonic() < deadline:
            if record_path.exists():
                try:
                    current = json.loads(record_path.read_text())
                except json.JSONDecodeError:
                    current = {}
                if current.get("pid") == host.pid:
                    reclaimed = True
                    break
            if host.poll() is not None:
                output = host.stdout.read() if host.stdout else ""
                break
            time.sleep(0.05)
    finally:
        _kill_and_reap(host)

    assert _ALREADY_RUNNING not in output, (
        "host start was refused by a stale daemon record whose PID was recycled "
        f"to a live non-omnigent process (pid={recycled_pid}); the record's owner "
        f"is dead, so startup must treat it as stale and proceed:\n{output}"
    )
    assert reclaimed, (
        "host neither reclaimed the stale recycled-PID daemon record nor kept "
        f"running; unexpected exit output:\n{output}"
    )


def test_stop_treats_recycled_foreign_pid_record_as_stale(
    tmp_path: Path,
    hanging_server: str,
    recycled_pid: int,
) -> None:
    """``omnigent stop`` must not crash when the recycled PID is another user's.

    The kernel answers EPERM when signalling a PID owned by another user —
    proof the recorded PID is not our daemon. ``omnigent stop`` (the recovery
    the "already running" error points operators at) must treat that record as
    stale — warn, drop it, exit cleanly — never propagate the
    ``PermissionError``. EPERM is injected at the kill(2) boundary via an
    LD_PRELOAD shim, since an unprivileged test cannot create a process under
    another uid.
    """
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.skip("no C compiler available to build the kill(2) EPERM shim")
    shim_src = tmp_path / "eperm_kill.c"
    shim_lib = tmp_path / "eperm_kill.so"
    shim_src.write_text(_EPERM_SHIM_SRC)
    subprocess.run(
        [cc, "-shared", "-fPIC", "-o", str(shim_lib), str(shim_src), "-ldl"],
        check=True,
        capture_output=True,
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _cli_env(state_dir, tmp_path / "home")
    record_path = _stage_stale_record(hanging_server, env, state_dir, recycled_pid)

    stop_env = dict(env)
    stop_env["LD_PRELOAD"] = str(shim_lib)
    stop_env["EPERM_PID"] = str(recycled_pid)
    result = subprocess.run(
        [sys.executable, "-m", "omnigent", "stop"],
        env=stop_env,
        capture_output=True,
        text=True,
        timeout=_STOP_TIMEOUT_S,
    )
    output = result.stdout + result.stderr

    assert "PermissionError" not in output and "Traceback" not in output, (
        "`omnigent stop` crashed on the EPERM from the recycled foreign-owned "
        f"PID instead of treating the record as stale:\n{output}"
    )
    assert result.returncode == 0, (
        f"`omnigent stop` exited {result.returncode} on a stale recycled-PID "
        f"daemon record:\n{output}"
    )
    assert not record_path.exists(), (
        "stale recycled-PID daemon record survived `omnigent stop`; the next "
        "`omnigent host` start would still be refused with "
        f"'{_ALREADY_RUNNING}':\n{output}"
    )
