"""Exercise terminal liveness with real tmux and a shell standing in for agy.

No LLM, agy binary, or Omnigent server is required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

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
_INPUT_READER = (
    'printf "ready\\n"; while IFS= read -r line; do printf "%s\\n" "$line" >> received.txt; done'
)
_SIGNATURE = "tmux unavailable after 3 consecutive probes for terminal antigravity:main"


@pytest.fixture
def terminal() -> Iterator[TerminalInstance]:
    # Short paths avoid the macOS Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix="og-agy-", dir="/tmp") as directory:
        short_dir = Path(directory)
        instance = TerminalInstance(
            name="antigravity",
            session_key="main",
            socket_path=short_dir / "tmux.sock",
            private_dir=short_dir,
            command="sh",
            args=["-c", _INPUT_READER],
        )
        try:
            asyncio.run(instance.launch(cwd=short_dir))
            yield instance
        finally:
            asyncio.run(instance.close())


@pytest.mark.parametrize("death", ["server", "missing-socket", "empty-server", "session"])
def test_external_tmux_death_reports_the_required_terminal_exit(
    terminal: TerminalInstance, caplog: pytest.LogCaptureFixture, death: str
) -> None:
    exit_fired = threading.Event()
    healthy_tick = threading.Event()
    with caplog.at_level(logging.WARNING, logger=terminal_mod.__name__):
        terminal.start_idle_watcher_thread(
            on_exit=exit_fired.set, on_tick=healthy_tick.set, poll_interval_s=0.05
        )
        assert healthy_tick.wait(_TIMEOUT_S)
        if death in {"empty-server", "session"}:
            terminal._tmux_output_sync("set-option", "-g", "exit-empty", "off")
            if death == "session":
                terminal._tmux_output_sync("new-session", "-d", "-s", "other", "sleep 60")
            terminal._tmux_output_sync("kill-session", "-t", terminal.tmux_target)
        else:
            terminal._tmux_output_sync("kill-server")
            if death == "missing-socket":
                terminal.socket_path.unlink(missing_ok=True)

        assert exit_fired.wait(_TIMEOUT_S), "watcher did not report terminal exit"
    assert not terminal.running
    assert any(_SIGNATURE in record.getMessage() for record in caplog.records)


def _install_tmux_shim(directory: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> str:
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    shim_dir = directory / "shim-bin"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text("#!/bin/sh\n" + script + f'exec {shlex.quote(real_tmux)} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    return real_tmux


@pytest.fixture
def probe_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    outage_flag = tmp_path / "outage-active"
    probes = tmp_path / "failed-probes"
    real_tmux = _install_tmux_shim(
        tmp_path,
        monkeypatch,
        f"if [ -e {shlex.quote(str(outage_flag))} ]; then\n"
        f"  echo probe >> {shlex.quote(str(probes))}\n"
        '  echo "error connecting to socket (Connection timed out)" >&2\n'
        "  exit 1\n"
        "fi\n",
    )
    monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_START_FAILURE_BACKOFF_SECONDS", 0.05)
    return outage_flag, probes, real_tmux


def test_transient_tmux_probe_outage_must_not_kill_a_live_terminal(
    terminal: TerminalInstance, probe_outage: tuple[Path, Path, str]
) -> None:
    outage_flag, probes, real_tmux = probe_outage
    exit_fired = threading.Event()
    healthy_tick = threading.Event()
    try:
        terminal.start_idle_watcher_thread(
            on_exit=exit_fired.set, on_tick=healthy_tick.set, poll_interval_s=0.05
        )
        assert healthy_tick.wait(_TIMEOUT_S)
        outage_flag.touch()
        # Each failed tick probes capture-pane and has-session.
        required_probes = 2 * (terminal_mod._IDLE_EXIT_FAILURE_THRESHOLD + 1)
        deadline = time.monotonic() + _TIMEOUT_S
        while not probes.exists() or len(probes.read_text().splitlines()) < required_probes:
            assert not exit_fired.is_set(), "temporary probe failure ended a live terminal"
            assert time.monotonic() < deadline, "watcher stopped retrying probes"
            time.sleep(0.02)

        session_alive = subprocess.run(
            [
                real_tmux,
                "-S",
                str(terminal.socket_path),
                "has-session",
                "-t",
                terminal.tmux_target,
            ],
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
        assert session_alive.returncode == 0
        healthy_tick.clear()
        outage_flag.unlink()
        assert healthy_tick.wait(_TIMEOUT_S), "watcher did not recover after the outage"
        assert not exit_fired.is_set()
        assert terminal.running
    finally:
        terminal._stop_idle_watcher_thread()
        outage_flag.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_partial_send_reports_error_without_replaying_input(
    terminal: TerminalInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_sent = shlex.quote(str(tmp_path / "first-sent"))
    failed_once = shlex.quote(str(tmp_path / "failed-once"))
    _install_tmux_shim(
        tmp_path,
        monkeypatch,
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "send-keys" ]; then\n'
        f"    if [ ! -e {first_sent} ]; then\n"
        f"      touch {first_sent}\n"
        f"    elif [ ! -e {failed_once} ]; then\n"
        f"      touch {failed_once}\n"
        '      echo "error connecting to socket (Connection timed out)" >&2\n'
        "      exit 1\n"
        "    fi\n"
        "    break\n"
        "  fi\n"
        "done\n",
    )
    # Keep the input below the platform's canonical line-length limit.
    monkeypatch.setattr(terminal_mod, "_SEND_KEYS_LITERAL_CHARS_PER_CALL", 64)
    prefix = "x" * terminal_mod._SEND_KEYS_LITERAL_CHARS_PER_CALL
    received = terminal.private_dir / "received.txt"
    result = await terminal.send(prefix + "unsent_suffix")
    assert "error" in result
    assert "Connection timed out" in result["error"]
    assert terminal.running
    assert not received.exists(), "send submitted Enter despite a failed text chunk"

    assert await terminal.send(keys="Enter") == {"status": "sent"}
    async with asyncio.timeout(_TIMEOUT_S):
        while not received.exists() or not received.read_text().endswith("\n"):
            await asyncio.sleep(0.02)
    assert received.read_text() == prefix + "\n"
    assert await terminal.send("recovered") == {"status": "sent"}
    async with asyncio.timeout(_TIMEOUT_S):
        while len(received.read_text().splitlines()) < 2:
            await asyncio.sleep(0.02)
    assert received.read_text() == prefix + "\nrecovered\n"


@pytest.mark.asyncio
async def test_runner_keeps_required_terminal_resource_during_probe_outage(
    tmp_path: Path, probe_outage: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    outage_flag, probes, _ = probe_outage
    monkeypatch.setattr(terminal_mod, "_IDLE_POLL_INTERVAL_SECONDS", 0.05)
    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=terminals)
    session_id = "tmux-outage-session"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="http://server",
    ) as server_client:
        app = create_runner_app(
            server_client=server_client, terminal_registry=terminals, resource_registry=resources
        )
        events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        app.state.session_event_queues[session_id] = events
        try:
            view = await resources.launch_required_terminal(
                session_id,
                "antigravity",
                "main",
                TerminalEnvSpec(
                    command="sh",
                    args=["-c", _INPUT_READER],
                    os_env=OSEnvSpec(
                        type="caller_process",
                        cwd=str(tmp_path),
                        sandbox=OSEnvSandboxSpec(type="none"),
                    ),
                ),
            )
            terminal = terminals.get(session_id, "antigravity", "main")
            assert terminal is not None
            async with asyncio.timeout(_TIMEOUT_S):
                while "ready" not in (terminal.last_pane_text() or ""):
                    await asyncio.sleep(0.02)
            outage_flag.touch()
            required_probes = 2 * (terminal_mod._IDLE_EXIT_FAILURE_THRESHOLD + 1)
            async with asyncio.timeout(_TIMEOUT_S):
                while (
                    not probes.exists() or len(probes.read_text().splitlines()) < required_probes
                ):
                    await asyncio.sleep(0.02)
            assert "error" in await terminal.read()
            assert await terminal.is_alive()
            assert terminal.running
            assert events.empty(), "temporary failure published a terminal/session exit"

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                resource_url = f"/v1/sessions/{session_id}/resources/{view.id}"
                assert (await client.get(resource_url)).status_code == 200
                outage_flag.unlink()
                assert await terminal.send("recovered") == {"status": "sent"}
                async with asyncio.timeout(_TIMEOUT_S):
                    while "recovered" not in (terminal.last_pane_text() or ""):
                        await asyncio.sleep(0.02)
                assert (await client.get(resource_url)).status_code == 200
                assert (await terminal.read()).get("screen") is not None

                await terminal._tmux("kill-server")
                observed = []
                async with asyncio.timeout(_TIMEOUT_S):
                    while not any(event.get("type") == "session.status" for event in observed):
                        observed.append(await events.get())
                deleted = [
                    event for event in observed if event["type"] == "session.resource.deleted"
                ]
                assert len(deleted) == 1
                assert deleted[0]["resource_id"] == view.id
                assert observed[-1] == {"type": "session.status", "status": "idle"}
                assert (await client.get(resource_url)).status_code == 404
                await resources.wait_for_terminal_exit_cleanup()
        finally:
            outage_flag.unlink(missing_ok=True)
            await resources.cleanup_session(session_id)
            app.state.session_event_queues.pop(session_id, None)
