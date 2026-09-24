"""A native session's tmux terminal must not outlive stop and uninstall.

Reported bug: a native session's tmux-backed terminal (a private-socket tmux
server running the vendor CLI) and its harness children **outlive**
``omnigent stop`` and ``omnigent uninstall --purge``. On a real host the reaper
that should tear the terminal down never completes, so the detached tmux server
survives every teardown command and keeps writing to ``~/.omnigent``.

The graceful path already reaps correctly: while the runner is alive,
``omnigent stop`` runs the daemon -> runner -> ``tmux kill-server`` chain and the
terminal dies. The leak only appears when the reaper is missed. This test makes
that deterministic by killing the session's runner processes with SIGKILL --
the documented real failure (see the grace-period note in ``omnigent/chat.py``:
"the runner was SIGKILL'd before tmux sessions were reaped, leaving zombie
codex/claude processes") -- so no graceful teardown runs. It then drives the
real ``omnigent stop`` and ``omnigent uninstall --purge`` and asserts the
managed tmux server and its child are gone afterward.

Neither ``omnigent stop`` nor ``omnigent uninstall`` currently has a backstop
that reaps an orphaned managed terminal: ``stop`` only signals the daemon, and
the uninstaller's tmux sweep (``scripts/uninstall_oss.sh``) matches only
sessions named ``omnigent:*`` on tmux's default socket, whereas managed
terminals use a private per-instance socket with session name ``main``. So on
the current build this test is RED (the server survives both commands); a fix
that reaps orphaned managed terminals on stop/uninstall turns it GREEN.

Requires ``claude`` and ``tmux`` on PATH; the terminal only needs to *boot*
(no Claude login / response), so this is gated on binary presence alone.
"""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pexpect
import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="needs `claude` and `tmux` on PATH to launch a native tmux terminal",
)

_TERMINAL_GLOB = "omnigent-terminal-*"


def _terminal_sockets() -> set[str]:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), _TERMINAL_GLOB, "tmux.sock")))


def _tmux_server_alive(socket_path: str) -> bool:
    return (
        subprocess.run(
            ["tmux", "-S", socket_path, "list-sessions"],
            capture_output=True,
        ).returncode
        == 0
    )


def _pane_pids(socket_path: str) -> list[int]:
    result = subprocess.run(
        ["tmux", "-S", socket_path, "list-panes", "-a", "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    return [int(tok) for tok in result.stdout.split() if tok.strip().isdigit()]


def _runner_pids() -> set[int]:
    """Pids of the session runner processes (they own the terminal reaper)."""
    # -ww disables ps's default 80-column truncation; without a tty the long
    # venv python path alone overruns it and hides ``omnigent.runner._zygote``.
    result = subprocess.run(["ps", "-ww", "-eo", "pid,args"], capture_output=True, text=True)
    pids: set[int] = set()
    for line in result.stdout.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and "omnigent.runner" in parts[1]:
            pids.add(int(parts[0]))
    return pids


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _drain(child: pexpect.spawn) -> None:
    try:
        child.read_nonblocking(size=1 << 16, timeout=1)
    except (pexpect.TIMEOUT, pexpect.EOF):
        pass


@pytest.fixture
def scratch_home(tmp_path: Path) -> Iterator[dict[str, str]]:
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": os.getcwd(),
    }
    env.pop("OMNIGENT", None)
    # Keep state in the scratch HOME's ~/.omnigent like a real install; the
    # suite-wide OMNIGENT_DATA_DIR override relocates the daemon and masks the
    # orphaned-terminal leak this test targets.
    env.pop("OMNIGENT_DATA_DIR", None)
    sockets_before = _terminal_sockets()
    yield env
    # Best-effort teardown: whatever the product failed to reap, we clean up so
    # a leaked tmux server/child never escapes into the shared CI box.
    for socket_path in _terminal_sockets() - sockets_before:
        for pid in _pane_pids(socket_path):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        subprocess.run(["tmux", "-S", socket_path, "kill-server"], capture_output=True)


def test_native_terminal_reaped_by_stop_and_uninstall(scratch_home: dict[str, str]) -> None:
    omnigent = shutil.which("omnigent")
    assert omnigent is not None
    workdir = os.path.join(scratch_home["HOME"], "work")

    sockets_before = _terminal_sockets()

    child = pexpect.spawn(
        omnigent,
        ["claude", "--use-native-config"],
        env=scratch_home,
        cwd=workdir,
        dimensions=(40, 120),
        timeout=300,
    )

    socket_path: str | None = None
    deadline = time.time() + 300
    try:
        while time.time() < deadline and child.isalive():
            for candidate in sorted(_terminal_sockets() - sockets_before):
                if os.path.exists(candidate) and _tmux_server_alive(candidate):
                    socket_path = candidate
                    break
            if socket_path:
                break
            _drain(child)
            time.sleep(1)

        assert socket_path is not None, "native session never created a live tmux terminal"
        time.sleep(10)  # let the pane's vendor CLI finish booting
        pane_pids = _pane_pids(socket_path)
        assert pane_pids, "managed terminal has no pane process"

        # Model the reaper miss: the terminal reaper runs inside the session
        # runner, so SIGKILL every runner (polling a window to catch late
        # spawns) with no graceful teardown, then drop the CLI client. This is
        # the documented real failure (see omnigent/chat.py). The detached tmux
        # server (remain-on-exit / exit-empty off) then has no owner left to
        # reap it -- exactly the orphaned state the report describes.
        killed: set[int] = set()
        kill_deadline = time.time() + 45
        while time.time() < kill_deadline:
            runners = _runner_pids()
            for pid in runners:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                    killed.add(pid)
            if killed and not _runner_pids():
                break
            time.sleep(2)
        assert killed, "no session runner was found to SIGKILL (reaper-miss precondition not set up)"
        child.kill(signal.SIGKILL)
        child.close(force=True)
        time.sleep(5)

        # Precondition: the detached tmux server survives its runner's death.
        assert _tmux_server_alive(socket_path), (
            "expected the managed tmux server to survive the runner's death "
            "(it is detached); the reaper-miss precondition did not hold"
        )
    finally:
        if child.isalive():
            child.kill(signal.SIGKILL)
            child.close(force=True)

    stop = subprocess.run(
        [omnigent, "stop"], env=scratch_home, cwd=workdir, capture_output=True, text=True, timeout=180
    )
    assert stop.returncode == 0, f"omnigent stop failed: {stop.stdout}\n{stop.stderr}"
    time.sleep(8)

    uninstall = subprocess.run(
        [omnigent, "uninstall", "--purge", "--yes"],
        env=scratch_home,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert uninstall.returncode == 0, f"omnigent uninstall failed: {uninstall.stdout}\n{uninstall.stderr}"
    time.sleep(5)

    # The bug: the managed tmux server and its harness child outlive both
    # commands. A correct teardown reaps them.
    assert not _tmux_server_alive(socket_path), (
        "managed tmux terminal server outlived `omnigent stop` + "
        "`omnigent uninstall --purge`: no teardown reaps an "
        "orphaned managed terminal"
    )
    assert not any(_pid_alive(pid) for pid in pane_pids), (
        "native harness child process outlived `omnigent stop` + "
        "`omnigent uninstall --purge`"
    )
