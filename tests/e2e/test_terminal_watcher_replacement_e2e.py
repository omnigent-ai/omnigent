"""A replaced terminal watcher must not apply a tmux probe result that finished late.

The runner replaces a terminal's threaded watcher when the terminal moves to
another session (``transfer_terminal`` -> ``start_idle_watcher_thread(replace=True)``).
The stop path joins the old thread for a bounded time only, so a probe that is
still inside a tmux command keeps running next to the replacement. These tests
hold one real probe subprocess open across that window with a ``tmux`` shim on
PATH and then let it finish. No LLM, agent binary, or Omnigent server is needed.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import stat
import subprocess
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals import TerminalRegistry

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

_TIMEOUT_S = 10.0
_SOURCE_SESSION = "conv_before_transfer"
_TARGET_SESSION = "conv_after_transfer"
# The pane keeps printing so the activity watcher always has a real change to report.
_CHATTY_PANE = 'i=0; while :; do echo "tick $i"; i=$((i+1)); sleep 0.2; done'


@dataclass
class _StalledProbe:
    real_tmux: str
    stall: Path
    entered: Path

    def hold(self) -> None:
        self.stall.touch()

    def release(self) -> None:
        self.stall.unlink(missing_ok=True)


def _install_stalling_tmux_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    subcommand: str,
    late_result: str,
) -> _StalledProbe:
    """Put a ``tmux`` shim on PATH that parks the first *subcommand* call while a flag exists.

    Every other invocation execs the real tmux at once, so only the probe that
    was in flight when the flag appeared is held. On release the parked call
    runs *late_result* (shell) when given, else the real command.
    """
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    stall = tmp_path / "stall-active"
    claimed = tmp_path / "stall-claimed"
    entered = tmp_path / "stall-entered"
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        f"if [ -e {shlex.quote(str(stall))} ]; then\n"
        '  for arg in "$@"; do\n'
        f'    if [ "$arg" = {shlex.quote(subcommand)} ]; then\n'
        f"      if mkdir {shlex.quote(str(claimed))} 2>/dev/null; then\n"
        f"        touch {shlex.quote(str(entered))}\n"
        f"        while [ -e {shlex.quote(str(stall))} ]; do sleep 0.02; done\n"
        f"        {late_result}\n"
        "      fi\n"
        "      break\n"
        "    fi\n"
        "  done\n"
        "fi\n"
        f'exec {shlex.quote(real_tmux)} "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    return _StalledProbe(real_tmux=real_tmux, stall=stall, entered=entered)


async def _wait_for(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(_TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.02)


async def _next_event(
    queue: asyncio.Queue[dict[str, object]], event_type: str
) -> dict[str, object]:
    async with asyncio.timeout(_TIMEOUT_S):
        while True:
            event = await queue.get()
            if event.get("type") == event_type:
                return event


def _drain(queue: asyncio.Queue[dict[str, object]]) -> list[dict[str, object]]:
    drained: list[dict[str, object]] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


@dataclass
class _TransferredTerminal:
    terminal: TerminalInstance
    original_watcher: threading.Thread
    source_events: asyncio.Queue[dict[str, object]]
    target_events: asyncio.Queue[dict[str, object]]
    probe: _StalledProbe


async def _run_transfer_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: _StalledProbe,
    observe: Callable[[_TransferredTerminal], Coroutine[Any, Any, None]],
) -> None:
    """Launch a real terminal, park its watcher's probe, transfer it, then run *observe*."""
    monkeypatch.setattr(terminal_mod, "_IDLE_POLL_INTERVAL_SECONDS", 0.05)
    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=terminals)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="http://server",
    ) as server_client:
        app = create_runner_app(
            server_client=server_client, terminal_registry=terminals, resource_registry=resources
        )
        source_events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        target_events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        app.state.session_event_queues[_SOURCE_SESSION] = source_events
        app.state.session_event_queues[_TARGET_SESSION] = target_events
        try:
            view = await resources.launch_required_terminal(
                _SOURCE_SESSION,
                "agent",
                "main",
                TerminalEnvSpec(
                    command="sh",
                    args=["-c", _CHATTY_PANE],
                    os_env=OSEnvSpec(
                        type="caller_process",
                        cwd=str(tmp_path),
                        sandbox=OSEnvSandboxSpec(type="none"),
                    ),
                ),
            )
            terminal = terminals.get(_SOURCE_SESSION, "agent", "main")
            assert terminal is not None
            # The source-owned watcher is healthy before anything is stalled.
            await _next_event(source_events, "session.terminal.activity")
            original = terminal._idle_thread
            assert original is not None and original.is_alive()

            probe.hold()
            await _wait_for(probe.entered.exists)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                response = await client.post(
                    f"/v1/sessions/{_SOURCE_SESSION}/resources/terminals/{view.id}/transfer",
                    json={"target_session_id": _TARGET_SESSION},
                )
            assert response.status_code == 200, response.text
            assert terminals.get(_TARGET_SESSION, "agent", "main") is terminal
            # The bounded join expired: the old probe is still parked in the shim.
            assert original.is_alive()
            assert terminal._idle_thread is not None and terminal._idle_thread is not original

            await _next_event(target_events, "session.terminal.activity")
            assert terminal.running
            _drain(source_events)

            await observe(
                _TransferredTerminal(
                    terminal=terminal,
                    original_watcher=original,
                    source_events=source_events,
                    target_events=target_events,
                    probe=probe,
                )
            )
        finally:
            probe.release()
            await resources.cleanup_session(_TARGET_SESSION)
            await resources.cleanup_session(_SOURCE_SESSION)
            app.state.session_event_queues.pop(_SOURCE_SESSION, None)
            app.state.session_event_queues.pop(_TARGET_SESSION, None)


async def _events_within(
    queue: asyncio.Queue[dict[str, object]], seconds: float
) -> list[dict[str, object]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    seen: list[dict[str, object]] = []
    while (remaining := deadline - loop.time()) > 0:
        try:
            seen.append(await asyncio.wait_for(queue.get(), remaining))
        except TimeoutError:
            break
    return seen


@pytest.mark.asyncio
async def test_replaced_watcher_late_capture_does_not_report_to_previous_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _install_stalling_tmux_shim(
        tmp_path, monkeypatch, subcommand="capture-pane", late_result=""
    )

    async def observe(race: _TransferredTerminal) -> None:
        race.probe.release()
        await asyncio.to_thread(race.original_watcher.join, _TIMEOUT_S)
        assert not race.original_watcher.is_alive()
        stale = await _events_within(race.source_events, 1.0)
        assert stale == [], f"replaced watcher still reported to its previous owner: {stale}"
        assert race.terminal.running
        await _next_event(race.target_events, "session.terminal.activity")

    await _run_transfer_race(tmp_path, monkeypatch, probe, observe)


@pytest.mark.asyncio
async def test_replaced_watcher_late_dead_pane_reading_keeps_healthy_terminal_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parked pane-death probe answers "pane dead" after the terminal moved on.
    probe = _install_stalling_tmux_shim(
        tmp_path, monkeypatch, subcommand="list-panes", late_result="printf '1 0\\n'; exit 0"
    )

    async def observe(race: _TransferredTerminal) -> None:
        alive = subprocess.run(
            [
                race.probe.real_tmux,
                "-S",
                str(race.terminal.socket_path),
                "list-panes",
                "-t",
                race.terminal.tmux_target,
                "-F",
                "#{pane_dead}",
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=True,
        )
        assert alive.stdout.split() == ["0"]

        race.probe.release()
        await asyncio.to_thread(race.original_watcher.join, _TIMEOUT_S)
        assert not race.original_watcher.is_alive()
        assert race.terminal.running, (
            "a dead-pane reading from the replaced watcher marked the healthy terminal not running"
        )
        stale = await _events_within(race.source_events, 1.0)
        assert stale == [], f"replaced watcher still reported to its previous owner: {stale}"
        target = await _events_within(race.target_events, 1.0)
        assert not [e for e in target if e.get("type") == "session.resource.deleted"], target
        await _next_event(race.target_events, "session.terminal.activity")

    await _run_transfer_race(tmp_path, monkeypatch, probe, observe)
