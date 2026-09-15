"""End-to-end repro: a leaked tracked head pid wedges the host orphan reaper.

A long-lived ``omnigent host`` daemon runs as a child subreaper, so every tool
subprocess a dying runner leaves behind (``git`` / ``tmux`` / ``node`` / …) is
reparented onto the host and must be reaped by ``_orphan_reaper_loop``. On the
reported host it instead accumulated **18,503 zombie children** over ~6 days and
then collapsed to ~11 *in one instant* during a server reconnect — proof the
orphans were reapable all along and the sweep was simply wedged.

Three defects in ``host/connect.py`` combine to disable the sweep indefinitely:

* **D1 — head-of-line ``break``.** ``_reap_orphans_waitid`` peeks the next
  reapable child with ``os.waitid(WNOWAIT)``; if that pid is in
  ``_tracked_runner_pids()`` it ``break``s, assuming the runner's own watcher
  reaps it within ~0.5s. Because ``WNOWAIT`` keeps returning the *same* head
  pid, one bad head pid starves every reap behind it — forever.
* **D2 — leaked ``_runners`` entries.** ``_watch_runner`` returns on both the
  clean-exit and crash paths without popping the entry, so ``_tracked_runner_pids()``
  keeps claiming pids of long-dead runners.
* **D3 — pid-reuse collision.** A dead runner's pid is freed (its watcher's
  ``poll()`` reaped it) while the stale entry (D2) still claims it. A fresh
  detached orphan spawned onto that reused pid becomes a permanent head-of-queue
  zombie that D1 refuses to reap and no watcher will ever consume.

This test drives the **real** ``HostProcess`` reaper against a **real** process
table. It reconstructs the D2+D3 wedge state directly (no multi-day wait / real
pid-counter wrap needed): a leaked ``_runners`` entry — its ``returncode``
already set, its pid freed — claiming a reused-pid zombie at the head of the
kernel's reapable-child queue, with genuine orphans queued behind it. It asserts
the reaper drains the orphans behind that head::

    .venv/bin/python -m pytest tests/e2e/test_host_orphan_reaper_tracked_head_wedge.py -v

On the buggy build the sweep ``break``s on the leaked head and reaps **zero**
(all zombies survive) — the assertion fails, reproducing the wedge. Once the
sweep resolves a stale/reused tracked head instead of breaking blind (prune the
leaked entry and continue), the identical orphans drain and the test passes. A
control phase mirrors the reported "drains only on reconnect": emptying the
tracked set (a reconnect's ``_cleanup_runners``) makes the same sweep drain
everything in one pass, proving the survivors were reapable all along.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from omnigent.host.connect import HostProcess, _RunnerHandle, _install_child_subreaper
from omnigent.host.identity import HostIdentity

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not hasattr(os, "waitid")
    or not hasattr(os, "fork"),
    reason="exercises the Linux/POSIX os.waitid reaper path against forked zombies",
)

# Orphans queued behind the wedged head. Sized so a single starved sweep is
# unambiguous, while staying tiny enough to reap instantly once unblocked.
_ORPHAN_COUNT = 8
# How many sweeps the reaper loop gets. On the buggy build every sweep exits at
# the same head pid (WNOWAIT never consumes it), so more sweeps never help.
_SWEEPS = 5


def _fork_zombie() -> int:
    """Fork a direct child that exits immediately, becoming a zombie of us.

    A zombie is a reapable child: exactly what the host's orphan reaper exists
    to drain (a real reparented ``git``/``tmux``/``node`` orphan is reapable in
    the same way once its runner dies).

    :returns: The child pid, now awaiting reaping.
    """
    pid = os.fork()
    if pid == 0:  # child
        os._exit(0)
    return pid


def _is_zombie(pid: int) -> bool:
    """Return whether *pid* is a zombie (``<defunct>``) child of this process.

    Reads ``/proc/<pid>/stat`` state field; a reaped pid's proc entry is gone.

    :param pid: The child pid to inspect.
    :returns: ``True`` while the child is an un-reaped zombie.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            # state is the first token after the ")"-terminated comm field.
            return fh.read().split(b")", 1)[1].split()[0] == b"Z"
    except FileNotFoundError:
        return False


class _LeakedRunnerProc:
    """Popen-shaped stand-in for a leaked ``_runners`` entry (D2 + D3).

    Models a runner whose watcher already observed its exit — ``returncode`` is
    set and the pid was reaped and freed — but whose ``_runners`` entry was
    never popped, so it keeps claiming a pid the kernel has since reused for a
    detached orphan. The reaper reads only ``.pid`` off tracked handles
    (``_tracked_runner_pids`` / ``_runner_handle_for_pid``); a correct sweep
    additionally inspects ``.returncode`` / ``.poll()`` to detect that the entry
    is stale, so this stand-in exposes all three.

    :param pid: The (reused) pid the leaked entry still claims.
    :param returncode: The already-observed exit code (non-``None`` => stale).
    """

    def __init__(self, pid: int, returncode: int = 0) -> None:
        self.pid = pid
        self.returncode: int | None = returncode

    def poll(self) -> int | None:
        """Return the cached exit code, matching ``Popen.poll`` on a dead proc."""
        return self.returncode


def _make_host() -> HostProcess:
    """Build a real ``HostProcess`` without touching the network or harnesses.

    :returns: A ``HostProcess`` whose reaper methods can be driven directly.
    """
    host = HostProcess(
        HostIdentity(host_id="host_reaper_wedge", name="repro-host"),
        "https://app.example.databricks.com",
        interactive_shells=["bash"],
    )
    # Skip live capability discovery — irrelevant here and it would probe local
    # harnesses on startup.
    host._capabilities_initialized = True
    return host


@pytest.mark.timeout(120)
def test_orphan_reaper_not_wedged_by_leaked_tracked_head(tmp_path: Path) -> None:
    """A leaked/reused tracked head pid must not starve orphan reaping.

    Journey: host daemon (subreaper) -> a runner self-exits and its
    ``_runners`` entry leaks (D2) -> its pid is freed and reused by a fresh
    detached orphan that exits (D3), landing a tracked-pid zombie at the head of
    the reap queue -> genuine orphans queue behind it. The orphan reaper must
    still drain those orphans.

    On the buggy build ``_reap_orphans_waitid`` ``break``s on the leaked head
    (D1) and reaps zero across every sweep, so the orphans survive as zombies —
    this assertion fails, reproducing the permanent wedge. A sweep that resolves
    the stale/reused head (prune + continue) drains them and the test passes.
    """
    if not _install_child_subreaper():
        pytest.skip("cannot install PR_SET_CHILD_SUBREAPER; no orphan reaping to test")

    host = _make_host()
    tracked_child: int | None = None
    orphans: list[int] = []
    try:
        # HEAD: a reused-pid orphan that exited, forked FIRST so it is the
        # OLDEST reapable child — the pid os.waitid(WNOWAIT) keeps returning.
        tracked_child = _fork_zombie()
        # D2: leak an entry for a long-dead runner whose watcher already set its
        # returncode; D3: its pid was freed and reused by the orphan above, so
        # the entry now claims a live zombie's pid.
        host._runners["runner_leaked"] = _RunnerHandle(
            proc=_LeakedRunnerProc(tracked_child, returncode=0),  # type: ignore[arg-type]
            log_path=tmp_path / "runner_leaked.log",
            session_id="conv_leaked",
        )

        # Genuine orphans queued BEHIND the wedged head. Every one is reapable.
        time.sleep(0.2)  # ensure the head is strictly the oldest reapable child
        orphans = [_fork_zombie() for _ in range(_ORPHAN_COUNT)]
        all_reapable = [tracked_child, *orphans]

        # Wait until the whole queue is actually reapable (zombies).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not all(_is_zombie(p) for p in all_reapable):
            time.sleep(0.02)
        assert all(_is_zombie(p) for p in all_reapable), (
            "test precondition failed: not every child became a zombie"
        )
        assert tracked_child in host._tracked_runner_pids(), (
            "test precondition failed: the leaked entry does not claim the head pid"
        )

        # Run the REAL reaper the way _orphan_reaper_loop does — repeated sweeps.
        reaped_while_wedged = 0
        for _ in range(_SWEEPS):
            reaped_while_wedged += host._reap_orphans_waitid()
        survivors = [p for p in all_reapable if _is_zombie(p)]

        # Control ("drains only on reconnect"): emptying the tracked set is what
        # a reconnect's _cleanup_runners does; the same sweep then drains
        # whatever survived — proving the survivors were reapable all along and
        # the only thing blocking them was the leaked tracked head.
        host._runners.clear()
        drained_on_reconnect = host._reap_orphans_waitid()
        after_reconnect = [p for p in all_reapable if _is_zombie(p)]

        # Sanity gate: this run only proves anything if the orphans were
        # genuinely reapable in this environment (they all drained across the
        # two phases). If not, the environment — not the bug — is at fault.
        assert reaped_while_wedged + drained_on_reconnect == len(all_reapable), (
            "environment did not behave as a subreaper: "
            f"{reaped_while_wedged} reaped while tracked + "
            f"{drained_on_reconnect} on reconnect != {len(all_reapable)} zombies"
        )
        assert not after_reconnect, (
            f"reconnect sweep left zombies behind: {after_reconnect}"
        )

        # The bug-specific assertion: the reaper must NOT be starved by the
        # leaked/reused tracked head. Fixed => it reaped every orphan behind the
        # head. Buggy => it broke on the head and reaped nothing, so the whole
        # queue survived until the tracked set was cleared.
        assert reaped_while_wedged == len(all_reapable), (
            "the orphan reaper wedged on a leaked/reused tracked head "
            f"pid ({tracked_child}). It reaped {reaped_while_wedged}/"
            f"{len(all_reapable)} reapable zombies across {_SWEEPS} sweeps, "
            f"leaving {survivors} un-reaped; the identical zombies then drained "
            f"({drained_on_reconnect}) the instant the tracked set was emptied — "
            "the head-of-line break starves all orphan reaping until reconnect."
        )
    finally:
        # Reap anything still outstanding so a failed run never leaks zombies
        # into the pytest worker's process table.
        host._runners.clear()
        for pid in ([tracked_child] if tracked_child is not None else []) + orphans:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
