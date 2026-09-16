"""Terminal watcher cancellation at probe and callback boundaries."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance


@pytest.mark.parametrize("boundary", ["capture", "confirmation", "pane_dead", "detach"])
@pytest.mark.parametrize("replace", [False, True])
def test_stopped_watcher_discards_in_flight_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, replace: bool
) -> None:
    """A probe returning after stop cannot fail a terminal or its new owner."""
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
        keep_alive_after_exit=True,
    )
    blocked = threading.Event()
    release = threading.Event()
    replacement_ticked = threading.Event()
    exits: list[str] = []
    monkeypatch.setattr(terminal_mod, "_IDLE_EXIT_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(terminal_mod, "_IDLE_WATCHER_JOIN_TIMEOUT_S", 0)

    def pause(at: str) -> None:
        if at == boundary:
            blocked.set()
            assert release.wait(5), "test did not release the probe"

    def capture() -> str | None:
        pause("capture")
        return None if boundary in {"capture", "confirmation"} else "final frame"

    def confirm() -> bool:
        pause("confirmation")
        return False

    def pane_dead() -> bool:
        pause("pane_dead")
        return True

    def detach(*args: str) -> str:
        assert args[0] == "detach-client"
        pause("detach")
        return ""

    monkeypatch.setattr(instance, "_capture_pane_for_idle_or_none", capture)
    monkeypatch.setattr(instance, "_tmux_session_exists_sync", confirm)
    monkeypatch.setattr(instance, "_pane_is_dead", pane_dead)
    monkeypatch.setattr(instance, "_tmux_output_sync", detach)
    instance.start_idle_watcher_thread(on_exit=lambda: exits.append("old"), poll_interval_s=0)
    old_thread = instance._idle_thread
    assert old_thread is not None
    try:
        assert blocked.wait(5), "watcher did not reach the probe boundary"
        if replace:
            monkeypatch.setattr(instance, "_capture_pane_for_idle_or_none", lambda: "live frame")
            monkeypatch.setattr(instance, "_pane_is_dead", lambda: False)
            instance.start_idle_watcher_thread(
                on_exit=lambda: exits.append("new"),
                on_tick=replacement_ticked.set,
                poll_interval_s=0.01,
                replace=True,
            )
            assert replacement_ticked.wait(5)
        else:
            instance._stop_idle_watcher_thread()
        release.set()
        old_thread.join(5)
        assert not old_thread.is_alive()
        assert instance.running
        assert exits == []
    finally:
        release.set()
        old_thread.join(5)
        new_thread = instance._idle_thread
        instance._stop_idle_watcher_thread()
        if new_thread is not None:
            new_thread.join(5)


def test_watcher_can_stop_itself_in_tick_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A callback can stop its watcher without self-join or later activity."""
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    completed = threading.Event()
    ready = threading.Event()
    activity: list[str] = []
    monkeypatch.setattr(instance, "_capture_pane_for_idle_or_none", lambda: "live")
    monkeypatch.setattr(instance, "_pane_is_dead", lambda: False)

    def on_tick() -> None:
        assert ready.wait(5)
        instance._stop_idle_watcher_thread()
        completed.set()

    instance.start_idle_watcher_thread(
        on_tick=on_tick, on_activity=lambda: activity.append("activity"), poll_interval_s=0.01
    )
    thread = instance._idle_thread
    assert thread is not None
    ready.set()
    try:
        assert completed.wait(5)
        thread.join(5)
        assert not thread.is_alive()
        assert activity == []
    finally:
        instance._stop_idle_watcher_thread()
        thread.join(5)
