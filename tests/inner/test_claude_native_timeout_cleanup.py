"""Exercise stale injection cleanup against actual native terminal generations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError

_TERMINAL = r"""
import os,sys,time,tty
from pathlib import Path
role,ready,accepted,release,completed=sys.argv[1:]
if role == "old":
    print("Synthetic terminal starting without its composer",flush=True)
else:
    print("─"*80+"\n❯ \n"+"─"*80,flush=True)
tty.setraw(sys.stdin.fileno())
Path(ready).write_text("ready")
while True:
    data=os.read(sys.stdin.fileno(),65536)
    if b"EXIT" in data:
        raise SystemExit(7)
    if role == "replacement" and b"START" in data:
        Path(accepted).write_text("1")
        while not Path(release).exists():time.sleep(0.01)
        Path(completed).write_text("1")
"""


def _tmux(socket: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", str(socket), "-f", "/dev/null", *arguments],
        text=True,
        capture_output=True,
        timeout=5,
    )


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize(
    "scenario,failure",
    [
        ("owned", "timeout"),
        ("replacement", "timeout"),
        ("renamed_session", "timeout"),
        ("same_session", "timeout"),
        ("respawned_pane", "timeout"),
        ("restarted_server", "timeout"),
        ("cross_process", "timeout"),
        ("unavailable", "timeout"),
        ("owned", "cancel"),
        ("replacement", "cancel"),
        ("owned", "exit"),
        ("replacement", "exit"),
    ],
)
async def test_stale_cleanup_preserves_a_same_owner_native_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    failure: str,
) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 0.2)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.01)
    monkeypatch.setenv(bridge.REQUEST_SESSION_ID_ENV_VAR, "same-conversation")
    if scenario == "unavailable":
        monkeypatch.setattr(bridge, "capture_tmux_terminal", lambda *_args, **_kwargs: None)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700)
    (bridge_dir / "bridge.json").write_text(json.dumps({"active_session_id": "same-conversation"}))
    terminal = tmp_path / "terminal.py"
    terminal.write_text(_TERMINAL)
    failure_observed = threading.Event()
    release_failure = threading.Event()
    original_wait = bridge._wait_for_claude_prompt_ready

    violations: list[str] = []

    def wait_for_old(socket_path, tmux_target, *, timeout_s, bridge_dir=None):
        if failure == "cancel":
            failure_observed.set()
            assert release_failure.wait(5)
            # The executor flags cancellation only once CancelledError reaches it,
            # which can trail task.cancel(); the drain swallows exceptions raised
            # here, so record the violation for the test body instead.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                bridge._check_injection_cancelled()
                time.sleep(0.01)
            violations.append("cancelled worker resumed delivery")
            raise AssertionError(violations[-1])
        try:
            original_wait(socket_path, tmux_target, timeout_s=0.05, bridge_dir=bridge_dir)
        except bridge.ClaudePromptTimeout:
            failure_observed.set()
            assert release_failure.wait(5)
            raise

    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", wait_for_old)
    with tempfile.TemporaryDirectory(prefix="claude-generation-", dir="/tmp") as sockets:
        old_socket = Path(sockets) / "old.sock"
        replacement_socket = Path(sockets) / "replacement.sock"
        sockets_to_close = {old_socket, replacement_socket}

        def command(role: str) -> str:
            return shlex.join(
                [
                    sys.executable,
                    str(terminal),
                    role,
                    str(tmp_path / (role + "-ready")),
                    str(tmp_path / "accepted"),
                    str(tmp_path / "release"),
                    str(tmp_path / "completed"),
                ]
            )

        def spawn(socket: Path, role: str) -> None:
            result = _tmux(
                socket,
                "new-session",
                "-d",
                "-s",
                "main",
                "-x",
                "100",
                "-y",
                "24",
                command(role),
            )
            assert result.returncode == 0, result.stderr

        async def turn():
            return [
                event
                async for event in ClaudeNativeExecutor(bridge_dir).run_turn(
                    messages=[{"role": "user", "content": "MUST_NOT_SEND"}],
                    tools=[],
                    system_prompt="",
                )
            ]

        task = None
        try:
            spawn(old_socket, "old")
            await _until((tmp_path / "old-ready").exists)
            bridge.write_tmux_target(bridge_dir, socket_path=old_socket, tmux_target="main")
            if failure == "exit":
                assert _tmux(old_socket, "set-option", "remain-on-exit", "on").returncode == 0
                assert (
                    _tmux(old_socket, "send-keys", "-t", "main", "EXIT", "Enter").returncode == 0
                )
                await _until(
                    lambda: (
                        _tmux(
                            old_socket, "display-message", "-p", "-t", "main", "#{pane_dead}"
                        ).stdout.strip()
                        == "1"
                    )
                )
            task = asyncio.create_task(turn())
            await _until(failure_observed.is_set)
            if failure == "cancel":
                task.cancel()
                await _until(task.cancelling)
            has_replacement = scenario not in {"owned", "unavailable"}
            replacement_target = "main"
            old_pane = _tmux(
                old_socket, "display-message", "-p", "-t", "main", "#{pane_id}"
            ).stdout.strip()
            if has_replacement:
                if scenario == "renamed_session":
                    assert _tmux(old_socket, "rename-session", "-t", "main", "old").returncode == 0
                    replacement_socket = old_socket
                    spawn(replacement_socket, "replacement")
                elif scenario == "same_session":
                    replacement_socket = old_socket
                    replacement_target = "main:replacement.0"
                    assert (
                        _tmux(
                            old_socket,
                            "new-window",
                            "-d",
                            "-t",
                            "main",
                            "-n",
                            "replacement",
                            command("replacement"),
                        ).returncode
                        == 0
                    )
                elif scenario == "respawned_pane":
                    replacement_socket = old_socket
                    assert (
                        _tmux(
                            old_socket, "respawn-pane", "-k", "-t", "main", command("replacement")
                        ).returncode
                        == 0
                    )
                elif scenario == "restarted_server":
                    assert _tmux(old_socket, "kill-server").returncode == 0
                    await _until(
                        lambda: _tmux(old_socket, "has-session", "-t", "main").returncode != 0
                    )
                    replacement_socket = old_socket
                    spawn(replacement_socket, "replacement")
                else:
                    spawn(replacement_socket, "replacement")
                await _until((tmp_path / "replacement-ready").exists)
                assert bridge._claude_prompt_rendered(
                    bridge._capture_pane(str(replacement_socket), replacement_target)
                )
                if scenario == "cross_process":
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; import sys; "
                            "from omnigent.harnesses.claude_native import bridge; "
                            "bridge._TRUSTED_PARENT = bridge._BRIDGE_ROOT = Path(sys.argv[3]); "
                            "bridge.write_tmux_target(Path(sys.argv[1]), "
                            "socket_path=Path(sys.argv[2]), tmux_target='main')",
                            str(bridge_dir),
                            str(replacement_socket),
                            str(tmp_path),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    assert result.returncode == 0, result.stderr
                else:
                    bridge.write_tmux_target(
                        bridge_dir, socket_path=replacement_socket, tmux_target=replacement_target
                    )
                assert bridge.read_active_session_id(bridge_dir) == "same-conversation"
                assert (
                    _tmux(
                        replacement_socket, "send-keys", "-t", replacement_target, "START", "Enter"
                    ).returncode
                    == 0
                )
                await _until((tmp_path / "accepted").exists)
            release_failure.set()
            if failure == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                assert violations == []
            else:
                events = await asyncio.wait_for(task, timeout=5)
                assert len(events) == 1 and isinstance(events[0], ExecutorError)
                assert events[0].undelivered
                assert ("has exited" if failure == "exit" else "did not become ready") in events[
                    0
                ].message
            if has_replacement:
                assert _tmux(replacement_socket, "has-session", "-t", "main").returncode == 0
                assert (tmp_path / "accepted").read_text() == "1"
                assert not (tmp_path / "completed").exists()
                (tmp_path / "release").touch()
                await _until((tmp_path / "completed").exists)
                assert (tmp_path / "completed").read_text() == "1"
                # Cleanup still retired the pane it observed; a respawned pane or a
                # restarted server reuses identifiers, so only these scenarios check.
                if scenario in {"replacement", "cross_process"}:
                    assert _tmux(old_socket, "has-session", "-t", "main").returncode != 0
                elif scenario == "renamed_session":
                    assert _tmux(old_socket, "has-session", "-t", "old").returncode != 0
                elif scenario == "same_session":
                    panes = _tmux(old_socket, "list-panes", "-s", "-t", "main", "-F", "#{pane_id}")
                    assert old_pane not in panes.stdout.split()
            elif scenario == "owned":
                assert _tmux(old_socket, "has-session", "-t", "main").returncode != 0
            else:
                assert _tmux(old_socket, "has-session", "-t", "main").returncode == 0
        finally:
            release_failure.set()
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            elif task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    task.result()
            for socket in sockets_to_close:
                _tmux(socket, "kill-server")
