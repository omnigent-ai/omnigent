"""Regression test: concurrent native launches must not start duplicate host daemons.

The race (before the fix):
  _ensure_host_daemon() did a check-then-spawn sequence with no coordination:
  1. _reuse_existing_daemon_record(target) -> reads the record -> sees no live daemon
  2. _spawn_host_daemon_process(...)        -> spawns a daemon subprocess
  3. _persist_spawned_daemon(...)           -> CLI writes the record

  When two CLI invocations both reached step 1 before either reached step 3,
  both saw "no daemon" and both spawned.  Each then called _persist_spawned_daemon;
  the second writer's record clobbered the first's, leaving the first daemon
  with no record: running, invisible to the registry, and un-stoppable (orphaned).

After the fix (daemon-level election):
  The daemon process itself acquires an exclusive flock (try_acquire) and writes
  its own record only after winning.  The loser daemon sees a held lock
  (BlockingIOError) and exits without writing.  The CLI polls _wait_for_daemon_claim
  until a live record appears.  Result: exactly one record whose PID is alive.

Test design:
  The mock intercepts _write_daemon_record to count writes.  The winner writes
  its record (write 1).  On unfixed code, _persist_spawned_daemon then writes for
  the winner (write 2) and the loser (write 3, the clobber).  On fixed code,
  _wait_for_daemon_claim finds the existing record and the loser path never writes.

  The loser mock holds until write 2 has landed (winner's _persist_spawned_daemon),
  then returns so the loser's _persist_spawned_daemon runs deterministically LAST on
  unfixed code.  On fixed code there is no write 2 (no _persist_spawned_daemon), so
  the loser waits for just write 1 (winner mock's write) and proceeds.

  The threshold is: loser proceeds when the write count is >= 2 on unfixed code
  (which has _persist_spawned_daemon), or >= 1 on fixed code (which doesn't).
  We detect this by using a short timeout: wait up to 100ms for a second write;
  if none arrives, proceed anyway (fixed code path).

  Assertion: the surviving record must have _PID_A (winner, alive).
"""

from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from omnigent import cli
from omnigent.cli import _ensure_host_daemon

_PID_A = 7771  # first-entry spawner (winner)
_PID_B = 7772  # second-entry spawner (loser)


@pytest.fixture(autouse=True)
def _stable_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from the developer's real host identity file."""
    monkeypatch.setattr(cli, "_load_existing_host_id", lambda: "host_abc")


def test_concurrent_ensure_host_daemon_does_not_leave_orphaned_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two concurrent _ensure_host_daemon calls must not orphan a running daemon.

    Direct reproduction of the concurrent-launch race: both invocations race
    past the liveness check before any record is written.

    The winning daemon (PID _PID_A) writes its record.  The losing daemon
    (PID _PID_B) exits without writing.  After both threads complete, the
    surviving record must contain _PID_A (alive), not _PID_B (dead).
    """
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")

    server_url = "https://racing.example.com"
    target = cli._normalize_daemon_target(server_url)

    # Intercept _write_daemon_record to track writes and control ordering.
    # write_events[i] is set when the i-th write has completed.
    write_events: list[threading.Event] = [threading.Event() for _ in range(5)]
    write_count = 0
    write_count_lock = threading.Lock()

    original_write = cli._write_daemon_record

    def _intercepted_write(record: cli._HostDaemonRecord) -> None:
        nonlocal write_count
        original_write(record)
        with write_count_lock:
            write_count += 1
            idx = write_count - 1
        if idx < len(write_events):
            write_events[idx].set()

    monkeypatch.setattr(cli, "_write_daemon_record", _intercepted_write)

    # Gate: hold all spawners until both have passed the liveness check.
    first_entered = threading.Event()
    both_entered = threading.Event()
    spawn_proceed = threading.Event()

    entry_counter = 0
    entry_lock = threading.Lock()

    def _racing_spawn(
        *,
        args: list[str],
        env: dict[str, str],
    ) -> cli._SpawnedDaemonProcess:
        """Hold all spawners until the race is set up, then model the election.

        Winner (entry 1, PID _PID_A): writes its record then returns.

        Loser (entry 2, PID _PID_B): waits up to 100ms for write 2 (winner's
        _persist_spawned_daemon call on unfixed code).  If write 2 arrives, the
        loser proceeds after; if not (fixed code has no write 2), the loser
        proceeds after write 1.  Either way, on unfixed code the loser's
        _persist_spawned_daemon always runs AFTER the winner's.

        :param args: Daemon argv.
        :param env: Daemon environment.
        :returns: Fake spawned-process metadata.
        """
        nonlocal entry_counter

        with entry_lock:
            entry_counter += 1
            my_entry = entry_counter

        if my_entry == 1:
            first_entered.set()
            my_pid = _PID_A
        else:
            both_entered.set()
            my_pid = _PID_B

        spawn_proceed.wait(timeout=5)

        if my_entry == 1:
            # Winner: write own record (mirrors elected daemon after try_acquire).
            cli._write_daemon_record(
                cli._HostDaemonRecord(
                    pid=my_pid,
                    target=target,
                    mode="server",
                    server_url=target,
                    log_path=str(tmp_path / "daemon.log"),
                    started_at=0,
                    host_id="host_abc",
                    config_sig=None,
                )
            )
            # write_events[0] is now set (write 1 done)
        else:
            # Loser: wait for write 2 (winner's _persist_spawned_daemon on unfixed
            # code) or fall back to write 1 (fixed code has no write 2).
            # 100ms is plenty for _persist_spawned_daemon to run if it exists.
            write_events[1].wait(timeout=0.1)
            # After this, the loser's subsequent code (either _wait_for_daemon_claim
            # on fixed code or _persist_spawned_daemon on unfixed code) runs
            # deterministically after the winner's _persist_spawned_daemon.

        return cli._SpawnedDaemonProcess(pid=my_pid, log_path=str(tmp_path / "daemon.log"))

    monkeypatch.setattr(cli, "_spawn_host_daemon_process", _racing_spawn)
    # Winner PID is alive; loser PID is dead (exited after losing the election).
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == _PID_A)
    monkeypatch.setattr(cli, "_daemon_host_identity_changed", lambda record: False)
    monkeypatch.setattr(cli, "_daemon_tunnel_recovers", lambda record, **kw: True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_ensure_host_daemon, server_url)

        assert first_entered.wait(timeout=5), "thread-1 never entered _spawn_host_daemon_process"
        f2 = pool.submit(_ensure_host_daemon, server_url)

        assert both_entered.wait(timeout=5), (
            "thread-2 never entered _spawn_host_daemon_process; "
            "it may have found the winner's record already; race not reproduced"
        )

        spawn_proceed.set()

        f1.result(timeout=10)
        f2.result(timeout=10)

    # --- assertion: surviving record must contain the winner's PID ---
    # With the bug:  the loser's _persist_spawned_daemon ran after the winner's
    #                (guaranteed by the write_events[1] gate) and clobbered the
    #                record; record.pid == _PID_B (dead loser); _PID_A (alive winner)
    #                has no record: orphaned.
    # With the fix:  only _wait_for_daemon_claim is called (no _persist_spawned_daemon);
    #                it finds the winner's record; record.pid == _PID_A (alive).
    registry_dir = tmp_path / "daemons"
    records = list(registry_dir.glob("*.json")) if registry_dir.exists() else []
    assert len(records) == 1, (
        f"Expected exactly 1 daemon registry record, found {len(records)}: {records}"
    )

    record = cli._read_daemon_record(records[0])
    assert record is not None, "registry record is unreadable"
    assert record.pid == _PID_A, (
        f"Expected winner PID {_PID_A} in the registry record, got {record.pid}.\n"
        f"The bug: _persist_spawned_daemon(pid={_PID_B}) ran after "
        f"_persist_spawned_daemon(pid={_PID_A}) and clobbered the record.\n"
        f"_PID_A ({_PID_A}) is running but has no registry entry: orphaned."
    )
