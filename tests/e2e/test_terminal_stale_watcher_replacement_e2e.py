"""A stopped/replaced terminal watcher must not clobber its healthy replacement.

A threaded idle watcher can finish a slow
tmux probe after ``start_idle_watcher_thread(replace=True)`` has stopped it (its
bounded join expires) and started a replacement. The stale thread then applies
its late probe result to the shared ``TerminalInstance``: it fires an obsolete
callback bound to the previous owner, or a dead-pane result marks the healthy
replacement's terminal not running.

Drives real tmux with a shell standing in for a native agent CLI, the real
``start_idle_watcher_thread(replace=True)`` teardown/restart path, and the real
``_idle_watch_loop_threaded`` probe loop. No LLM, agent binary, or Omnigent
server is required. Both cases fail on unchanged main.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

_TIMEOUT_S = 10.0
# A shell that stays alive so real capture-pane / pane-death probes see a
# healthy pane throughout the race.
_LONG_LIVED = 'printf "ready\\n"; while :; do sleep 0.05; done'


@pytest.fixture
def terminal() -> Iterator[TerminalInstance]:
    # Short paths avoid the macOS Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix="og-watch-", dir="/tmp") as directory:
        short_dir = Path(directory)
        instance = TerminalInstance(
            name="antigravity",
            session_key="main",
            socket_path=short_dir / "tmux.sock",
            private_dir=short_dir,
            command="sh",
            args=["-c", _LONG_LIVED],
        )
        try:
            asyncio.run(instance.launch(cwd=short_dir))
            yield instance
        finally:
            asyncio.run(instance.close())


def _block_first_call(
    instance: TerminalInstance,
    method_name: str,
    *,
    entered: threading.Event,
    release: threading.Event,
    poisoned_result: object,
    poison: bool,
) -> None:
    """Make the first call to ``method_name`` block until ``release``.

    The first caller (the original watcher's in-flight probe) parks on the
    barrier; every later caller (the replacement watcher) delegates to the real
    probe against the healthy tmux server. On release the parked call returns
    ``poisoned_result`` when ``poison`` is set, else the real probe's result.
    Instance-dict functions are not descriptors, so the loop's ``self.<probe>()``
    calls this plain wrapper with no bound ``self``.
    """
    real: Callable[..., object] = getattr(instance, method_name)
    claimed = threading.Event()
    lock = threading.Lock()

    def wrapper(*args: object, **kwargs: object) -> object:
        with lock:
            first = not claimed.is_set()
            if first:
                claimed.set()
        if first:
            entered.set()
            release.wait(_TIMEOUT_S)
            if poison:
                return poisoned_result
        return real(*args, **kwargs)

    setattr(instance, method_name, wrapper)


def test_stale_watcher_dead_pane_result_must_not_stop_healthy_replacement(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_mod, "_IDLE_WATCHER_JOIN_TIMEOUT_S", 0.2)
    stale_exit = threading.Event()
    live_exit = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    # Original watcher's pane-death probe blocks in flight; on release it
    # returns a stale "pane dead" reading for a pane that is actually healthy.
    _block_first_call(
        terminal,
        "_pane_is_dead",
        entered=entered,
        release=release,
        poisoned_result=True,
        poison=True,
    )
    terminal.start_idle_watcher_thread(on_exit=stale_exit.set, poll_interval_s=0.05)
    assert entered.wait(_TIMEOUT_S), "original watcher never entered the pane-death probe"

    # Replace the watcher: its bounded join expires (the old probe is parked),
    # and the replacement begins polling the healthy terminal.
    terminal.start_idle_watcher_thread(
        on_exit=live_exit.set, poll_interval_s=0.05, replace=True
    )
    assert terminal.running

    # Release the stale probe. Its result must be discarded because its own stop
    # event was set while it ran.
    release.set()
    stale_exit.wait(_TIMEOUT_S)

    assert terminal.running, "stale watcher marked the healthy replacement terminal not running"
    assert not stale_exit.is_set(), "stale watcher fired the previous owner's exit callback"
    assert not live_exit.is_set()

    terminal._stop_idle_watcher_thread()


def test_stale_watcher_must_not_fire_previous_owner_callback_after_replacement(
    terminal: TerminalInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_mod, "_IDLE_WATCHER_JOIN_TIMEOUT_S", 0.2)
    stale_tick = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    # Original watcher's capture probe blocks in flight; on release it returns a
    # real healthy snapshot so the stale loop proceeds to its tick callback.
    _block_first_call(
        terminal,
        "_capture_pane_for_idle_or_none",
        entered=entered,
        release=release,
        poisoned_result=None,
        poison=False,
    )
    terminal.start_idle_watcher_thread(on_tick=stale_tick.set, poll_interval_s=0.05)
    assert entered.wait(_TIMEOUT_S), "original watcher never entered the capture probe"

    terminal.start_idle_watcher_thread(
        on_tick=lambda: None, poll_interval_s=0.05, replace=True
    )
    assert terminal.running

    # The original watcher can only reach its tick callback after this release,
    # i.e. after it was stopped and replaced — any such firing is obsolete.
    release.set()
    stale_tick.wait(_TIMEOUT_S)

    assert not stale_tick.is_set(), (
        "stale watcher fired the previous owner's tick callback after replacement"
    )

    terminal._stop_idle_watcher_thread()
