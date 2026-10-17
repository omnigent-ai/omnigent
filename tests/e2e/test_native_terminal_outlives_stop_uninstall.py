"""A native session's tmux terminal must not outlive `omnigent stop` and `omnigent uninstall`.

Drives the real local-mode CLI in a scratch HOME: `omnigent claude` spawns the
local server, host daemon and runner, and the runner launches Claude Code in a
detached tmux server on a private socket (``$TMPDIR/omnigent-terminal-*/tmux.sock``,
session ``main``). While the runner is alive the graceful stop chain reaps that
server; the leak needs a runner that died without graceful teardown, which this
test stages with SIGKILL before running the two teardown commands.

Requires ``claude`` and ``tmux`` on PATH; Claude only has to boot against the
mock model, no account is needed.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.e2e._native_resume_helpers import (
    PtyHandle,
    omnigent_console_script,
    spawn_cli_background,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="needs Linux /proc plus `claude` and `tmux` on PATH to launch a native tmux terminal",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOT_TIMEOUT_S = 180.0
_TEARDOWN_TIMEOUT_S = 30.0
_STRIP_EXACT = frozenset(
    {
        "OMNIGENT",
        "OMNIGENT_DATA_DIR",
        "OMNIGENT_CONFIG_HOME",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "TMUX",
        "RUNNER_SERVER_URL",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
        "PYTEST_ADDOPTS",
    }
)
_STRIP_PREFIXES = ("OMNIGENT_RUNNER_", "OMNIGENT_HOST_", "OMNIGENT_COMPAT_")


class _ScratchInstall:
    """A short-path scratch HOME holding a real local install's state."""

    def __init__(self, mock_llm_url: str) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="o8867-"))
        self.home = self.root / "home"
        self.work = self.home / "work"
        self.tmp = self.root / "tmp"
        config_home = self.root / "cfg"
        claude_dir = self.root / "claude"
        shim_bin = self.root / "bin"
        for directory in (
            self.work,
            self.tmp,
            config_home,
            claude_dir,
            shim_bin,
            self.home / ".omnigent",
        ):
            directory.mkdir(parents=True)
        (self.home / ".omnigent" / "installation_id").write_text("e2e-install\n")
        # The scratch install is not a uv tool; a no-op uv keeps the uninstall's
        # wheel step from failing on hosts without uv.
        (shim_bin / "uv").write_text("#!/bin/sh\nexit 0\n")
        (shim_bin / "uv").chmod(0o755)
        (config_home / "config.yaml").write_text(
            "providers:\n"
            "  e2e-claude:\n"
            "    kind: key\n"
            "    default: [anthropic]\n"
            "    anthropic:\n"
            f'      base_url: "{mock_llm_url}"\n'
            '      api_key: "mock-key"\n'
            "      models:\n"
            "        default: claude-sonnet-4-20250514\n"
        )
        (claude_dir / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "projects": {str(self.work): {"hasTrustDialogAccepted": True}},
                }
            )
        )
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in _STRIP_EXACT and not key.startswith(_STRIP_PREFIXES)
        }
        self.env.update(
            {
                "HOME": str(self.home),
                "TMPDIR": str(self.tmp),
                "PATH": f"{shim_bin}:{os.environ.get('PATH', '')}",
                "OMNIGENT_CONFIG_HOME": str(config_home),
                "CLAUDE_CONFIG_DIR": str(claude_dir),
                "PYTHONPATH": os.pathsep.join(
                    p for p in (str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")) if p
                ),
                "TERM": "xterm-256color",
                "LINES": "40",
                "COLUMNS": "160",
                "OMNIGENT_NO_UPDATE_CHECK": "1",
                "OMNIGENT_SKIP_ONBOARD": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        for key in ("NO_PROXY", "no_proxy"):
            self.env[key] = ",".join(filter(None, (self.env.get(key), "localhost,127.0.0.1,::1")))

    def terminal_sockets(self) -> list[str]:
        return sorted(glob.glob(str(self.tmp / "omnigent-terminal-*" / "tmux.sock")))

    def instance_dirs(self) -> list[str]:
        return sorted(glob.glob(str(self.tmp / "omnigent-terminal-*")))

    def processes(self) -> list[tuple[int, str]]:
        """Pids launched under this scratch HOME (daemon, server, runner, claude, hooks)."""
        marker = f"HOME={self.home}".encode()
        found: list[tuple[int, str]] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                if marker not in Path(f"/proc/{entry}/environ").read_bytes().split(b"\0"):
                    continue
                cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            found.append((int(entry), cmdline.decode(errors="replace")))
        return found

    def run_cli(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(omnigent_console_script()), *args],
            env=self.env,
            cwd=str(self.work),
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def _tmux(socket_path: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", socket_path, *args], capture_output=True, text=True, timeout=15
    )


def _server_alive(socket_path: str) -> bool:
    return os.path.exists(socket_path) and _tmux(socket_path, "list-sessions").returncode == 0


def _live_pane_pids(socket_path: str) -> list[int]:
    result = _tmux(socket_path, "list-panes", "-a", "-F", "#{pane_pid} #{pane_dead}")
    return [
        int(pid)
        for pid, dead in (line.split() for line in result.stdout.splitlines())
        if dead == "0"
    ]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def _terminal_gone(socket_path: str, pane_pids: list[int]) -> bool:
    return not _server_alive(socket_path) and not any(_pid_alive(pid) for pid in pane_pids)


def _wait_for_terminal(install: _ScratchInstall, handle: PtyHandle) -> tuple[str, list[int]]:
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        for socket_path in install.terminal_sockets():
            if _server_alive(socket_path):
                pane_pids = _live_pane_pids(socket_path)
                if (
                    pane_pids
                    and "Claude Code"
                    in _tmux(socket_path, "capture-pane", "-p", "-t", "main").stdout
                ):
                    return socket_path, pane_pids
        if not _pid_alive(handle.pid):
            break
        time.sleep(1)
    raise AssertionError(
        f"`omnigent claude` produced no live Claude tmux terminal within {_BOOT_TIMEOUT_S}s; "
        f"CLI output tail:\n{handle.output()[-2000:]}"
    )


@pytest.fixture
def scratch_install(isolated_mock_llm_server_url: str) -> Iterator[_ScratchInstall]:
    install = _ScratchInstall(isolated_mock_llm_server_url)
    try:
        yield install
    finally:
        # Housekeeping for whatever the product left behind, so a leaked tmux
        # server or harness child never escapes into the shared CI box.
        for socket_path in install.terminal_sockets():
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                _tmux(socket_path, "kill-server")
        for pid, _ in install.processes():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(install.root, ignore_errors=True)


def test_native_terminal_outlives_stop_and_uninstall(scratch_install: _ScratchInstall) -> None:
    install = scratch_install
    handle = spawn_cli_background(
        [str(omnigent_console_script()), "claude"], env=install.env, cwd=str(install.work)
    )
    try:
        socket_path, pane_pids = _wait_for_terminal(install, handle)

        # Stage the reaper miss: the session runner dies without graceful
        # teardown, leaving the detached tmux server with no owner to reap it.
        runner_pids = [pid for pid, cmd in install.processes() if "omnigent.runner" in cmd]
        assert runner_pids, "no session runner process found to SIGKILL"
        for pid in runner_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    finally:
        handle.terminate()
    time.sleep(5)
    assert _server_alive(socket_path), (
        "precondition: the detached tmux server should survive its runner's death"
    )
    assert all(_pid_alive(pid) for pid in pane_pids)

    stop = install.run_cli("stop", timeout=180)
    assert stop.returncode == 0, f"omnigent stop failed: {stop.stdout}\n{stop.stderr}"
    # Teardown can trail the command, so poll to a deadline instead of a fixed grace.
    _wait_until(lambda: _terminal_gone(socket_path, pane_pids), _TEARDOWN_TIMEOUT_S)
    server_after_stop = _server_alive(socket_path)
    claude_after_stop = [pid for pid in pane_pids if _pid_alive(pid)]

    uninstall = install.run_cli("uninstall", "--purge", "--yes", timeout=300)
    assert uninstall.returncode == 0, (
        f"omnigent uninstall failed: {uninstall.stdout}\n{uninstall.stderr}"
    )
    _wait_until(lambda: _terminal_gone(socket_path, pane_pids), _TEARDOWN_TIMEOUT_S)
    assert not (install.home / ".omnigent").exists()
    server_after_uninstall = _server_alive(socket_path)
    claude_after_uninstall = [pid for pid in pane_pids if _pid_alive(pid)]

    assert (
        not server_after_stop and not claude_after_stop and "orphaned terminal(s)" in stop.stdout
    ), (
        f"managed tmux terminal outlived `omnigent stop` (stdout: {stop.stdout.strip()!r}); "
        f"server alive={server_after_stop}, claude pids alive={claude_after_stop}"
    )
    assert (
        not server_after_uninstall and not claude_after_uninstall and not install.instance_dirs()
    ), (
        "managed tmux terminal outlived `omnigent uninstall --purge`; "
        f"server alive={server_after_uninstall}, claude pids alive={claude_after_uninstall}, "
        f"instance dirs={install.instance_dirs()}"
    )
