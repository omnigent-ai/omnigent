"""The startup orphan sweep must not reap live terminals it cannot place an owner for.

The runner's startup orphan sweep (:func:`reap_orphaned_terminals`) decides a
terminal tmux server is a leak by reading the owner pid recorded in its instance
dir and asking whether that pid is alive *here*. But a bare pid names a process
only inside the pid namespace + boot that minted it, while the instance dirs sit
in a temp root shared by every process on the box. When a starting runner reads
a marker written by another runner in a *different* pid namespace (or a previous
boot), that pid resolves to nothing locally, the sweep calls the terminal an
orphan, and it runs ``tmux kill-server`` on the socket a running agent is still
using.

The user then sees a session failed with "Required terminal exited
unexpectedly; the session runtime is no longer available." while the terminal
never actually exited (the runner logs the exit with *no* ``(exited with status
N)`` -- the pane was alive when the sweep decided it was gone).

This exercises the real sweep against a real, live tmux server whose owner-pid
marker names a pid that is not alive in this namespace -- the exact byte-level
state a foreign-namespace / legacy-bare-pid marker produces for the
namespace-blind sweep. The sweep must LEAVE the live server alone (absence of a
resolvable owner is not evidence of death). On the buggy build the sweep kills
the live server and removes the instance dir, so this fails; the fix makes it
pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance, reap_orphaned_terminals


def _tmux_server_reachable(socket_path: Path, target: str) -> bool:
    """Return whether the private tmux server/session is still reachable.

    :param socket_path: The instance's private tmux control socket.
    :param target: The tmux session target, e.g. ``"s1:main"``.
    :returns: ``True`` when ``has-session`` succeeds (server alive).
    """
    probe = subprocess.run(
        ["tmux", "-S", str(socket_path), "has-session", "-t", target],
        capture_output=True,
        timeout=5,
    )
    return probe.returncode == 0


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires a real tmux binary")
@pytest.mark.asyncio
async def test_orphan_sweep_keeps_live_terminal_with_unresolvable_owner_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup sweep must not reap a live terminal it cannot place an owner for.

    :param monkeypatch: Retargets the sweep's temp root at the scratch dir.
    """
    # A SHORT scratch root -- the tmux control socket path must stay under the
    # ~108-char AF_UNIX limit, so pytest's deeply-nested ``tmp_path`` can't be
    # used. Retarget the sweep's scan root here so it only ever considers our
    # own instance dir (the module exposes this indirection for exactly this
    # reason).
    scratch = Path(tempfile.mkdtemp(prefix="og-sweep-"))
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: scratch)

    # The dir name must carry the sweep's prefix so its glob matches it.
    instance_dir = scratch / f"{terminal_mod._TERMINAL_DIR_PREFIX}1"
    instance_dir.mkdir()
    socket_path = instance_dir / "tmux.sock"

    # A real, long-lived tmux terminal: stands in for a user's live agent CLI
    # (a running ``claude:main`` pane). Its true owner -- this test process --
    # is alive throughout.
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=socket_path,
        private_dir=instance_dir,
        command="sh",
        args=["-c", "sleep 600"],
        keep_alive_after_exit=True,
    )
    await instance.launch(cwd=instance_dir)
    try:
        # Wait for the private tmux server to come up.
        for _ in range(250):
            if _tmux_server_reachable(socket_path, instance.tmux_target):
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - only on a launch hang/regression
            raise AssertionError("tmux server never became reachable after launch")

        # Owner-pid marker that is NOT alive in this namespace: spawn a child,
        # let it exit, and reap it, then stamp its now-dead pid. This is
        # byte-identical to what the namespace-blind sweep reads for a marker
        # written by a runner in a *different* pid namespace / previous boot --
        # a bare integer that names no live process here, even though the tmux
        # server (and its real owner) is alive.
        dead = subprocess.Popen(["sh", "-c", "exit 0"])
        dead.wait()
        dead_pid = dead.pid
        assert not terminal_mod._process_alive(dead_pid), (
            f"precondition: pid {dead_pid} must be dead in this namespace "
            "(a reused pid would invalidate the repro; extremely rare, rerun)"
        )
        (instance_dir / terminal_mod._OWNER_PID_FILENAME).write_text(
            str(dead_pid), encoding="utf-8"
        )

        reaped = reap_orphaned_terminals()

        # The live tmux server must survive: an owner pid that cannot be placed
        # in this namespace is not evidence the terminal died. On the buggy
        # build the sweep reaps it (kill-server on the live socket + rmtree),
        # so all three assertions fail.
        assert reaped == 0, (
            "orphan sweep reaped a LIVE terminal whose owner pid is merely "
            f"unresolvable in this namespace (reaped={reaped})"
        )
        assert _tmux_server_reachable(socket_path, instance.tmux_target), (
            "orphan sweep killed the live tmux server -- a running agent's turn "
            "would fail with 'Required terminal exited unexpectedly'"
        )
        assert instance_dir.exists(), "orphan sweep removed a live terminal's instance dir"
    finally:
        # Under the bug the server is already gone; swallow teardown errors.
        with contextlib.suppress(Exception):
            await instance.close()
        shutil.rmtree(scratch, ignore_errors=True)
