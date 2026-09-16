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

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

_TIMEOUT_S = 10.0
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
            args=["-c", "while :; do sleep 1; done"],
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


def test_transient_tmux_probe_outage_must_not_kill_a_live_terminal(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    short_dir = terminal.private_dir
    shim_dir = short_dir / "shim-bin"
    shim_dir.mkdir()
    outage_flag = short_dir / "outage-active"
    probes = short_dir / "failed-probes"
    shim = shim_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        f"if [ -e {shlex.quote(str(outage_flag))} ]; then\n"
        f"  echo probe >> {shlex.quote(str(probes))}\n"
        '  echo "error connecting to socket (Connection timed out)" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'exec {shlex.quote(real_tmux)} "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(terminal_mod, "_TMUX_PROBE_START_FAILURE_BACKOFF_SECONDS", 0.05)
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
