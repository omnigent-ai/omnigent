"""An orphan sweep may act only on owner identities it can resolve."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
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
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(terminal_mod.subprocess, "run", fail)
    assert terminal_mod.reap_orphaned_terminals() == 0
    assert socket.exists()
    monkeypatch.setattr(
        terminal_mod.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
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
