"""An orphan sweep may act only on owner identities it can resolve."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.native import owner_claim


@pytest.mark.parametrize("failure", ["spawn", "timeout", "nonzero"])
def test_failed_reap_preserves_control_socket_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    directory = tmp_path / "omnigent-terminal-retry"
    directory.mkdir()
    socket = directory / "tmux.sock"
    socket.touch()
    owner_claim.write_owner_claim(directory)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)

    def fail(*args: object, **kwargs: object) -> SimpleNamespace:
        if failure == "spawn":
            raise OSError(errno.EAGAIN, "cannot fork")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("tmux", 10)
        return SimpleNamespace(returncode=1, stderr=b"permission denied")

    monkeypatch.setattr(terminal_mod.subprocess, "run", fail)
    assert terminal_mod.reap_orphaned_terminals() == 0
    assert socket.exists()
    monkeypatch.setattr(
        terminal_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b""),
    )
    assert terminal_mod.reap_orphaned_terminals() == 1
    assert not directory.exists()


def test_reap_removes_stale_socket_when_tmux_reports_server_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "omnigent-terminal-stale"
    directory.mkdir()
    socket = directory / "tmux.sock"
    socket.touch()
    owner_claim.write_owner_claim(directory)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)

    monkeypatch.setattr(
        terminal_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr=f"no server running on {socket}".encode()
        ),
    )

    assert terminal_mod.reap_orphaned_terminals() == 1
    assert not directory.exists()


@pytest.mark.parametrize(
    "record",
    [
        b"123456789",
        b"123456789\npid_ns=foreign\nboot=local-boot\n",
        b"123456789\npid_ns=local-ns\nboot=other-boot\n",
        b"\xff",
    ],
)
def test_sweep_preserves_unresolvable_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: bytes
) -> None:
    directory = tmp_path / "omnigent-terminal-owned"
    directory.mkdir()
    (directory / "owner.pid").write_bytes(record)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    monkeypatch.setattr(owner_claim, "current_pid_namespace", lambda: "local-ns")
    monkeypatch.setattr(owner_claim, "current_boot_id", lambda: "local-boot")

    assert terminal_mod.reap_orphaned_terminals() == 0
    assert directory.exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("ownership", ["legacy", "foreign", "dead_local"])
def test_sweep_only_kills_real_tmux_with_proven_dead_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ownership: str
) -> None:
    """A live server survives even when its owner's PID is not locally visible."""
    directory = tmp_path / "omnigent-terminal-live"
    directory.mkdir()
    record = "123456789\n"
    if ownership != "legacy":
        namespace = "local-ns" if ownership == "dead_local" else "foreign"
        record += f"pid_ns={namespace}\nboot=local-boot\n"
    (directory / "owner.pid").write_text(record)
    base = ["tmux", "-S", str(directory / "tmux.sock"), "-f", os.devnull]
    subprocess.run(
        [*base, "new-session", "-d", "-s", "main", "sleep 60"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    monkeypatch.setattr(owner_claim, "current_pid_namespace", lambda: "local-ns")
    monkeypatch.setattr(owner_claim, "current_boot_id", lambda: "local-boot")
    try:
        assert terminal_mod.reap_orphaned_terminals() == (1 if ownership == "dead_local" else 0)
        result = subprocess.run(
            [*base, "has-session", "-t", "main"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        assert (result.returncode == 0) is (ownership != "dead_local")
    finally:
        subprocess.run([*base, "kill-server"], check=False, capture_output=True, timeout=10)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_sweep_kills_sighup_ignoring_pane_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep SIGKILLs a pane child that ignores the SIGHUP kill-server sends.

    A native harness CLI (e.g. claude) traps SIGHUP to survive disconnects, so
    ``tmux kill-server`` alone leaves it running; the reaper must force-kill the
    pane's process group.
    """
    directory = tmp_path / "omnigent-terminal-harness"
    directory.mkdir()
    owner_claim.write_owner_claim(directory)
    base = ["tmux", "-S", str(directory / "tmux.sock"), "-f", os.devnull]
    subprocess.run(
        [*base, "new-session", "-d", "-s", "main", "trap '' HUP; sleep 300"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    pane_pid = int(
        subprocess.run(
            [*base, "list-panes", "-a", "-F", "#{pane_pid}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.split()[0]
    )
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    try:
        assert _alive(pane_pid)
        assert terminal_mod.reap_orphaned_terminals() == 1
        assert not directory.exists()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(pane_pid):
            time.sleep(0.1)
        assert not _alive(pane_pid), "SIGHUP-ignoring pane child survived the sweep"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(pane_pid), signal.SIGKILL)
        subprocess.run([*base, "kill-server"], check=False, capture_output=True, timeout=10)


def test_reap_preserves_dir_when_pane_snapshot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable pane snapshot preserves the dir instead of reaping blind.

    If the pane list cannot be read, killing the server and removing the dir
    would strand SIGHUP-ignoring children, so the sweep must not even run
    kill-server; it leaves the socket for a later retry.
    """
    directory = tmp_path / "omnigent-terminal-blind"
    directory.mkdir()
    socket = directory / "tmux.sock"
    socket.touch()
    owner_claim.write_owner_claim(directory)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    monkeypatch.setattr(terminal_mod, "_list_pane_pids", lambda socket_path: None)

    def _fail_run(*args: object, **kwargs: object) -> SimpleNamespace:
        raise AssertionError("kill-server must not run when the pane snapshot fails")

    monkeypatch.setattr(terminal_mod.subprocess, "run", _fail_run)
    assert terminal_mod.reap_orphaned_terminals() == 0
    assert socket.exists()


def test_reap_skips_pane_group_when_pid_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane pid reused after kill-server is not force-killed.

    The pane process's start time is captured before kill-server; if a later
    read returns a different start time the pid was recycled, so the sweep
    must leave that unrelated process group alone.
    """
    directory = tmp_path / "omnigent-terminal-reused"
    directory.mkdir()
    socket = directory / "tmux.sock"
    socket.touch()
    owner_claim.write_owner_claim(directory)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    monkeypatch.setattr(terminal_mod, "_list_pane_pids", lambda socket_path: [4242])
    monkeypatch.setattr(
        terminal_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    start_times = iter([100, 200])
    monkeypatch.setattr(terminal_mod, "_pid_start_time", lambda pid: next(start_times))
    killed: list[int] = []
    monkeypatch.setattr(terminal_mod.os, "killpg", lambda pgid, sig: killed.append(pgid))
    assert terminal_mod.reap_orphaned_terminals() == 1
    assert not directory.exists()
    assert killed == []


def test_reap_skips_pane_group_when_start_time_missing_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Linux a pane with no captured start time is left alone, not killed.

    A missing /proc start time means the pid was already gone when snapshotted,
    so force-killing its group could hit an unrelated process that reused it.
    """
    directory = tmp_path / "omnigent-terminal-nostart"
    directory.mkdir()
    socket = directory / "tmux.sock"
    socket.touch()
    owner_claim.write_owner_claim(directory)
    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(terminal_mod, "_process_alive", lambda pid: False)
    monkeypatch.setattr(terminal_mod, "IS_LINUX", True)
    monkeypatch.setattr(terminal_mod, "_list_pane_pids", lambda socket_path: [4242])
    monkeypatch.setattr(
        terminal_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    monkeypatch.setattr(terminal_mod, "_pid_start_time", lambda pid: None)
    killed: list[int] = []
    monkeypatch.setattr(terminal_mod.os, "killpg", lambda pgid, sig: killed.append(pgid))
    assert terminal_mod.reap_orphaned_terminals() == 1
    assert not directory.exists()
    assert killed == []
