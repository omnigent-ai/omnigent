"""A stopped watcher must not apply stale probe results after ownership transfer."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance


@pytest.mark.parametrize("blocked_probe", ["capture", "session", "pane"])
def test_replaced_watcher_discards_in_flight_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    blocked_probe: str,
) -> None:
    """A probe outliving the join timeout cannot fail the new watcher or notify its old owner."""
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    entered = threading.Event()
    release = threading.Event()
    replacement_ticked = threading.Event()
    old_exited = threading.Event()
    old_ticked = threading.Event()
    new_exited = threading.Event()
    origin: threading.Thread | None = None
    missing_probes = 0

    def pause() -> None:
        entered.set()
        assert release.wait(3.0), "test did not release the stopped watcher's probe"

    def capture() -> str | None:
        nonlocal origin
        current = threading.current_thread()
        if origin is None:
            origin = current
        if current is not origin:
            return "healthy replacement"
        if blocked_probe == "capture":
            pause()
        return None if blocked_probe == "session" else "old snapshot"

    def session_exists() -> bool:
        nonlocal missing_probes
        missing_probes += 1
        if missing_probes == terminal_mod._IDLE_EXIT_FAILURE_THRESHOLD:
            pause()
        return False

    def pane_dead() -> bool:
        if threading.current_thread() is origin and blocked_probe == "pane":
            pause()
            return True
        return False

    monkeypatch.setattr(instance, "_capture_pane_for_idle_or_none", capture)
    monkeypatch.setattr(instance, "_tmux_session_exists_sync", session_exists)
    monkeypatch.setattr(instance, "_pane_is_dead", pane_dead)
    monkeypatch.setattr(terminal_mod, "_IDLE_WATCHER_JOIN_TIMEOUT_S", 0.01)
    instance.start_idle_watcher_thread(
        on_exit=old_exited.set, on_tick=old_ticked.set, poll_interval_s=0.001
    )
    first_thread = instance._idle_thread
    assert first_thread is not None
    try:
        assert entered.wait(1.0)
        instance.start_idle_watcher_thread(
            on_exit=new_exited.set,
            on_tick=replacement_ticked.set,
            poll_interval_s=0.001,
            replace=True,
        )
        assert replacement_ticked.wait(1.0)
        release.set()
        first_thread.join(timeout=1.0)
        assert not first_thread.is_alive()
        assert instance.running, "the stopped watcher killed its healthy replacement"
        assert not old_exited.is_set()
        assert not old_ticked.is_set()
        assert not new_exited.is_set()
        assert not [
            r
            for r in caplog.records
            if r.name == terminal_mod.__name__ and r.levelno >= logging.ERROR
        ]
    finally:
        release.set()
        instance._stop_idle_watcher_thread()
        first_thread.join(timeout=1.0)
