"""
Host-service disable e2e: a stale ``launchctl print`` must not leak the plist.

Drives the real ``omnigent host enable`` / ``omnigent host disable`` CLI as a
subprocess — the exact commands a macOS user types — against a scripted
``launchctl`` that models launchd behavior measured on macOS:

- ``launchctl bootout`` returns immediately but unloads asynchronously, so
  ``launchctl print`` can still report the job during the unload window;
- once the job is gone, ``launchctl print`` exits 113;
- ``bootout`` never removes the plist file, only the loaded label.

``platform.system()`` is pinned to ``"Darwin"`` in the CLI subprocess via a
``sitecustomize`` module on ``PYTHONPATH``, so the launchd code path runs on
any CI host without a real launchd.

The user journey under guard::

    omnigent host enable                    # installs the RunAtLoad plist
    omnigent host disable && omnigent host enable --server <new>

When the stale-print window fires, ``host disable`` must still remove
``~/Library/LaunchAgents/ai.omnigent.host.plist``. If the plist survives, its
``RunAtLoad=true`` silently restores the previous server at the next login,
and the ``&&`` chain short-circuits so the service is never re-pointed.

Usage::

    python -m pytest tests/e2e/test_host_disable_stale_plist_e2e.py -v
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NEW_SERVER = "http://127.0.0.1:9"

# Scripted launchd control. bootstrap loads the label; bootout flips it to an
# asynchronous "unloading" state; print reports the job for the first
# LAUNCHCTL_STUB_PRINT_WINDOW calls of that window (the stale window), then
# exits 113 like a real absent service.
_LAUNCHCTL_STUB = """\
#!/usr/bin/env bash
STATE_DIR="${LAUNCHCTL_STUB_STATE:?LAUNCHCTL_STUB_STATE not set}"
WINDOW="${LAUNCHCTL_STUB_PRINT_WINDOW:-1}"
mkdir -p "$STATE_DIR"
case "$1" in
  bootstrap)
    echo loaded > "$STATE_DIR/state"
    rm -f "$STATE_DIR/print_count"
    exit 0
    ;;
  bootout)
    if [ -f "$STATE_DIR/state" ]; then
      echo unloading > "$STATE_DIR/state"
      echo 0 > "$STATE_DIR/print_count"
    fi
    exit 0
    ;;
  print)
    state=$(cat "$STATE_DIR/state" 2>/dev/null || echo absent)
    case "$state" in
      loaded) exit 0 ;;
      unloading)
        n=$(cat "$STATE_DIR/print_count" 2>/dev/null || echo 0)
        echo $((n + 1)) > "$STATE_DIR/print_count"
        if [ "$n" -lt "$WINDOW" ]; then
          exit 0
        fi
        echo absent > "$STATE_DIR/state"
        exit 113
        ;;
      *) exit 113 ;;
    esac
    ;;
  *) exit 0 ;;
esac
"""

# Imported at interpreter startup (site picks it up from PYTHONPATH), so the
# CLI subprocess takes the macOS launchd code path on any CI host.
_SITECUSTOMIZE = 'import platform\nplatform.system = lambda: "Darwin"\n'


@dataclass(frozen=True)
class _HostCliHarness:
    """Isolated home + env for driving ``omnigent host`` subprocesses."""

    home: Path
    env: dict[str, str]
    cwd: Path

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / "ai.omnigent.host.plist"

    def run_host(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``omnigent host <args>`` exactly as a user would."""
        return subprocess.run(
            [sys.executable, "-m", "omnigent.cli", "host", *args],
            env=self.env,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def plist_program_arguments(self) -> list[str]:
        payload = plistlib.loads(self.plist_path.read_bytes())
        return list(payload["ProgramArguments"])


@pytest.fixture
def host_cli(tmp_path: Path) -> _HostCliHarness:
    """Build an isolated macOS-shaped host-service environment."""
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(_LAUNCHCTL_STUB)
    launchctl.chmod(0o755)
    pysite = tmp_path / "pysite"
    pysite.mkdir()
    (pysite / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    pythonpath = os.pathsep.join(
        entry
        for entry in (
            str(pysite),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        )
        if entry
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": pythonpath,
        "LAUNCHCTL_STUB_STATE": str(tmp_path / "launchd-state"),
        # One stale `launchctl print` per unload: the asynchronous-bootout
        # window during which the job is still reported as running.
        "LAUNCHCTL_STUB_PRINT_WINDOW": "1",
    }
    return _HostCliHarness(home=home, env=env, cwd=cwd)


def test_disable_removes_plist_despite_async_unload_window(
    host_cli: _HostCliHarness,
) -> None:
    """``host disable`` must not leave the RunAtLoad plist behind.

    launchd unloads asynchronously, so a ``launchctl print`` issued right
    after ``bootout`` can still report the job. That stale answer must not
    abort the disable before the plist is unlinked: a surviving plist carries
    ``RunAtLoad=true`` and silently restores the host service at next login.
    """
    enable = host_cli.run_host("enable")
    assert enable.returncode == 0, f"host enable failed: {enable.stderr}"
    assert host_cli.plist_path.exists(), "enable did not install the plist"
    payload = plistlib.loads(host_cli.plist_path.read_bytes())
    assert payload["RunAtLoad"] is True

    disable = host_cli.run_host("disable")

    assert not host_cli.plist_path.exists(), (
        f"host disable (exit={disable.returncode}, stderr={disable.stderr!r}) "
        f"left {host_cli.plist_path} on disk; its RunAtLoad=true restores the "
        "host service at the next login"
    )
    assert disable.returncode == 0, (
        f"host disable exited {disable.returncode} on a job launchd was "
        f"still unloading: {disable.stderr}"
    )


def test_disable_enable_chain_repoints_service_despite_stale_print(
    host_cli: _HostCliHarness,
) -> None:
    """``host disable && host enable --server <new>`` must re-point the service.

    The natural replacement idiom chains the two commands with ``&&``. When
    the stale ``launchctl print`` aborts the disable, the chain
    short-circuits: enable never runs and the retained plist still points at
    the previous server, which comes back at next login.
    """
    enable = host_cli.run_host("enable")
    assert enable.returncode == 0, f"host enable failed: {enable.stderr}"
    old_args = host_cli.plist_program_arguments()

    # Shell `&&` semantics: the enable runs only when the disable succeeds.
    disable = host_cli.run_host("disable")
    if disable.returncode == 0:
        enable_new = host_cli.run_host("enable", "--non-interactive", "--server", _NEW_SERVER)
        assert enable_new.returncode == 0, f"host enable --server failed: {enable_new.stderr}"

    stale_plist_survived = (
        host_cli.plist_path.exists() and host_cli.plist_program_arguments() == old_args
    )
    assert not stale_plist_survived, (
        f"after `host disable && host enable --server {_NEW_SERVER}` the old "
        f"service definition survived at {host_cli.plist_path} (disable "
        f"exit={disable.returncode}, stderr={disable.stderr!r}); RunAtLoad "
        "restores the previous server at the next login"
    )
    assert host_cli.plist_path.exists() and _NEW_SERVER in host_cli.plist_program_arguments(), (
        "the disable && enable chain did not re-point the service at "
        f"{_NEW_SERVER} (disable exit={disable.returncode}, "
        f"stderr={disable.stderr!r})"
    )
