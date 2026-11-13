"""Tests for the cross-platform process/identity shims in ``omnigent._platform``."""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
import time

import psutil
import pytest

import omnigent
from omnigent import _platform


def test_current_user_tag_uses_uid_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_platform.os, "getuid", lambda: 501, raising=False)
    assert _platform.current_user_tag() == "501"


def test_current_user_tag_sanitizes_login_without_getuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(_platform.os, "getuid", raising=False)
    monkeypatch.setattr(_platform.getpass, "getuser", lambda: "CORP\\Jane Doe")
    assert _platform.current_user_tag() == "CORP_Jane_Doe"


def test_pid_exists_reports_live_and_dead_pids() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    assert _platform.pid_exists(os.getpid())
    assert not _platform.pid_exists(proc.pid)
    assert not _platform.pid_exists(0)


def test_pid_exists_uses_psutil_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 4242)

    def _no_kill(*_args: object) -> None:
        raise AssertionError("os.kill(pid, 0) sends CTRL_C_EVENT on Windows")

    monkeypatch.setattr(_platform.os, "kill", _no_kill)
    assert _platform.pid_exists(4242)
    assert not _platform.pid_exists(4243)


def _spawn_parent_with_child() -> tuple[subprocess.Popen[bytes], int]:
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        **_platform.detached_popen_kwargs(),
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    return parent, child_pid


def _wait_gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def test_kill_process_tree_kills_leader_and_descendants() -> None:
    parent, child_pid = _spawn_parent_with_child()
    try:
        _platform.kill_process_tree(parent.pid)
        parent.wait(timeout=10)
        assert _wait_gone(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
        if psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


def test_kill_process_tree_without_killpg_uses_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    parent, child_pid = _spawn_parent_with_child()
    monkeypatch.delattr(_platform.os, "killpg", raising=False)
    try:
        _platform.kill_process_tree(parent.pid)
        parent.wait(timeout=10)
        assert _wait_gone(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
        if psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


def test_kill_process_tree_missing_pid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    monkeypatch.delattr(_platform.os, "killpg", raising=False)
    with pytest.raises(ProcessLookupError):
        _platform.kill_process_tree(proc.pid)


def test_detached_popen_kwargs_matches_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert _platform.detached_popen_kwargs() == {"start_new_session": True}
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    assert _platform.detached_popen_kwargs() == {"creationflags": 0x200}


def test_tmux_missing_hint_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_platform, "IS_WINDOWS", False)
    assert _platform.tmux_missing_hint() == ""
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    assert "WSL" in _platform.tmux_missing_hint()


def test_every_omnigent_module_imports() -> None:
    """Guards against top-level POSIX-only imports (fcntl, termios, os.getuid, ...)."""
    failures: dict[str, str] = {}
    for info in pkgutil.walk_packages(omnigent.__path__, "omnigent."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:
            failures[info.name] = f"{type(exc).__name__}: {exc}"
    assert failures == {}
