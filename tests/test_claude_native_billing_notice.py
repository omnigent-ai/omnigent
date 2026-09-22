"""Only Claude's informational classifier-billing notice may receive an auto-Enter."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native import bridge

_RULE = "─" * 80
_BODY = """\
We're changing auto mode to no longer charge for classifier requests in Claude Code.
However, this session isn't eligible because your requests go through
gateway.example.com, which isn't compatible with this update.
Nothing breaks: auto mode keeps working, and its classifier requests are billed as before.
To fix it and access the new version of auto mode, ask your gateway to implement:
https://code.claude.com/docs/en/auto-mode-classifier-billing
Enter to continue · Esc to cancel
"""
_NOTICE = f"Earlier tool output\n{_RULE}\n{_BODY}"
_COMPOSER = f"{_RULE}\n❯ draft message\n{_RULE}\n? for shortcuts\n"
_PERMISSION = f"{_RULE}\nDo you want to run this command?\nEnter to select · Esc to cancel\n"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    result = _Clock()
    monkeypatch.setattr(bridge, "time", result)
    return result


@pytest.fixture
def bridge_dir(tmp_path: Path) -> Path:
    (tmp_path / "tmux.json").write_text(
        json.dumps({"socket_path": "/tmp/test.sock", "tmux_target": "claude:0.0"})
    )
    return tmp_path


def _terminal(monkeypatch: pytest.MonkeyPatch, frames: list[str]) -> Mock:
    remaining = iter(frames)
    current = frames[-1]

    def capture(*_args: object) -> str:
        nonlocal current
        current = next(remaining, current)
        return current

    sends = Mock()
    monkeypatch.setattr(bridge, "_capture_pane", capture)
    monkeypatch.setattr(bridge, "_run_tmux", sends)
    return sends


@pytest.mark.parametrize("width", [25, 80, 160])
def test_notice_matches_wrapped_text_and_gateway(width: int) -> None:
    body = _BODY.replace("gateway.example.com", "https://other.example.net:8443/api")
    pane = f"{_RULE}\n{textwrap.fill(body, width=width)}\n"
    assert bridge.auto_mode_billing_notice_visible(pane)


@pytest.mark.parametrize(
    "pane",
    [
        "",
        _BODY,
        _NOTICE.replace("billed as before", "billed at a new rate"),
        _NOTICE.replace("Enter to continue", "Enter to approve"),
        _NOTICE.replace("https://code.claude.com/docs/en/auto-mode-classifier-billing", ""),
        _NOTICE.replace("Esc to cancel", ""),
        _NOTICE + "\nDo you want to run this command?",
        _NOTICE + _COMPOSER,
        _NOTICE + _PERMISSION,
        _PERMISSION,
        f"{_RULE}\nSwitch model?\nEnter to continue · Esc to cancel",
    ],
)
def test_other_surfaces_and_transcript_quotes_do_not_match(pane: str) -> None:
    assert not bridge.auto_mode_billing_notice_visible(pane)


def test_acknowledges_notice_once_then_leaves_draft_untouched(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, clock: _Clock
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE, _NOTICE, _COMPOSER])
    assert bridge.acknowledge_auto_mode_billing_notice(bridge_dir)
    sends.assert_called_once_with("/tmp/test.sock", "send-keys", "-t", "claude:0.0", "Enter")
    assert clock.now >= bridge._CLAUDE_READY_POLL_INTERVAL_S


@pytest.mark.parametrize("replacement", ["", _COMPOSER, _PERMISSION])
def test_requires_fresh_notice_confirmation_before_enter(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, clock: _Clock, replacement: str
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE, replacement])
    assert not bridge.acknowledge_auto_mode_billing_notice(bridge_dir)
    sends.assert_not_called()


@pytest.mark.parametrize("replacement", ["", _COMPOSER, _PERMISSION])
def test_no_retry_enter_after_notice_disappears(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, clock: _Clock, replacement: str
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE, _NOTICE, replacement])
    assert bridge.acknowledge_auto_mode_billing_notice(bridge_dir)
    assert sends.call_count == 1


def test_swallowed_enter_retries_are_spaced_and_bounded(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, clock: _Clock
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE])
    times: list[float] = []
    sends.side_effect = lambda *_args: times.append(clock.now)
    assert bridge.acknowledge_auto_mode_billing_notice(bridge_dir)
    assert len(times) > 1
    assert all(
        later - earlier >= bridge._CONFIRM_DIALOG_RETRY_INTERVAL_S
        for earlier, later in pairwise(times)
    )
    assert clock.now < bridge._CONFIRM_DIALOG_ACCEPT_TIMEOUT_S + 0.2


def test_live_permission_hook_prevents_background_ack(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, clock: _Clock
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE])
    monkeypatch.setattr(bridge, "_has_approval_wait", lambda _path: True)
    assert not bridge.acknowledge_auto_mode_billing_notice(bridge_dir)
    sends.assert_not_called()


@pytest.mark.parametrize(
    ("expected_socket", "expected_target"),
    [("/tmp/another.sock", "claude:0.0"), ("/tmp/test.sock", "another:0.0")],
)
def test_stale_bridge_cannot_acknowledge_another_terminal(
    monkeypatch: pytest.MonkeyPatch,
    bridge_dir: Path,
    clock: _Clock,
    expected_socket: str,
    expected_target: str,
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE])
    assert not bridge.acknowledge_auto_mode_billing_notice(
        bridge_dir,
        expected_socket_path=expected_socket,
        expected_tmux_target=expected_target,
    )
    sends.assert_not_called()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--settings", "/tmp/random-bridge/claude-settings.json"], Path("/tmp/random-bridge")),
        (["--settings=/tmp/random-bridge/claude-settings.json"], Path("/tmp/random-bridge")),
        (["--settings", "/tmp/user-settings.json"], None),
        (["--settings", "claude-settings.json"], None),
        (["--settings"], None),
        ([], None),
    ],
)
def test_resolves_actual_bridge_from_launch_args(args: list[str], expected: Path | None) -> None:
    assert bridge.bridge_dir_from_launch_args(args) == expected


def test_background_ack_does_not_wait_for_message_writer(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, clock: _Clock
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE])
    with bridge._bridge_injection_lock(bridge_dir):
        assert not bridge.acknowledge_auto_mode_billing_notice(bridge_dir)
    sends.assert_not_called()


def test_background_ack_cannot_race_harness_process(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, clock: _Clock
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE])
    script = """\
import sys
from pathlib import Path
from omnigent.harnesses.claude_native.bridge import _serialize_bridge_injection

@_serialize_bridge_injection
def deliver(bridge_dir):
    print('locked', flush=True)
    sys.stdin.readline()

deliver(Path(sys.argv[1]))
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(bridge_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            assert process.stdout is not None
            assert process.stdout.readline() == "locked\n"
            assert not bridge.acknowledge_auto_mode_billing_notice(bridge_dir)
            sends.assert_not_called()
        finally:
            process.communicate("done\n", timeout=10)
    assert process.returncode == 0


@contextmanager
def _held_injection_lock(bridge_dir: Path, holder: str) -> Iterator[None]:
    if holder == "thread":
        with bridge._bridge_injection_lock(bridge_dir):
            yield
        return
    ready = bridge_dir / "writer-ready"
    script = """\
import sys
from pathlib import Path
from omnigent.harnesses.claude_native.bridge import _bridge_injection_file_lock

with _bridge_injection_file_lock(Path(sys.argv[1])):
    Path(sys.argv[2]).touch()
    sys.stdin.readline()
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(bridge_dir), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert process.poll() is None, "lock holder exited before acquiring its lock"
                assert time.monotonic() < deadline, "lock holder did not become ready"
                time.sleep(0.01)
            yield
            assert process.poll() is None, "lock holder exited before the waiter cancelled"
        finally:
            try:
                process.communicate("release\n", timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
                raise
    assert process.returncode == 0


@pytest.mark.parametrize("holder", ["thread", "process"])
def test_cancelled_writer_exits_before_lock_holder_releases(
    monkeypatch: pytest.MonkeyPatch, bridge_dir: Path, holder: str
) -> None:
    cancelled = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    errors: list[Exception] = []
    sends = Mock()
    monkeypatch.setattr(bridge, "_run_tmux", sends)

    def deliver() -> None:
        with bridge.cancellable_injection(cancelled):
            started.set()
            try:
                bridge.inject_user_message(bridge_dir, content="must not be delivered")
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

    worker = threading.Thread(target=deliver, daemon=True)
    try:
        with _held_injection_lock(bridge_dir, holder):
            worker.start()
            assert started.wait(2), "queued writer did not start"
            assert not finished.wait(0.15), "writer bypassed the held lock"
            cancelled.set()
            assert finished.wait(2), "cancelled writer still waited for the active writer"
            assert len(errors) == 1
            assert isinstance(errors[0], bridge.ClaudeInjectionCancelled)
            sends.assert_not_called()
    finally:
        cancelled.set()
        if worker.ident is not None:
            worker.join(timeout=5)
    assert not worker.is_alive()
    # Cancellation must release any lock the queued writer acquired first.
    lock = bridge._bridge_injection_lock(bridge_dir)
    assert lock.acquire(blocking=False), "cancelled writer leaked its thread lock"
    try:
        with bridge._bridge_injection_file_lock(bridge_dir).acquire(timeout=0):
            pass
    finally:
        lock.release()


def test_message_reclaim_acknowledges_notice_instead_of_cancelling(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE, _NOTICE, _NOTICE, _COMPOSER])
    bridge._restore_occupied_input("/tmp/test.sock", "claude:0.0")
    sends.assert_called_once_with("/tmp/test.sock", "send-keys", "-t", "claude:0.0", "Enter")


def test_readiness_wait_continues_after_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    sends = _terminal(monkeypatch, [_NOTICE, _NOTICE, _NOTICE, _COMPOSER])
    bridge._wait_for_claude_prompt_ready("/tmp/test.sock", "claude:0.0", timeout_s=1.0)
    assert sends.call_count == 1


@pytest.mark.parametrize("notice_after_switch", [False, True])
@pytest.mark.parametrize("ack_delay", [0.0, 2.4])
def test_permission_mode_change_acknowledges_notice_before_or_during_switch(
    monkeypatch: pytest.MonkeyPatch,
    bridge_dir: Path,
    clock: _Clock,
    notice_after_switch: bool,
    ack_delay: float,
) -> None:
    state = {"mode": "default", "notice": not notice_after_switch, "acknowledged": False}
    dismiss_at: float | None = None

    def capture(*_args: object) -> str:
        if dismiss_at is not None and clock.now >= dismiss_at:
            state["notice"] = False
        if state["notice"]:
            return _NOTICE
        footer = "auto mode on" if state["mode"] == "auto" else "manual mode on"
        return f"{_RULE}\n❯\n{_RULE}\n{footer}\n"

    keys: list[str] = []

    def send(*args: str) -> None:
        nonlocal dismiss_at
        key = args[-1]
        keys.append(key)
        if key == "Enter":
            assert state["notice"]
            if dismiss_at is None:
                dismiss_at = clock.now + ack_delay
            state["acknowledged"] = True
        elif key == "BTab":
            state["mode"] = "auto"
            state["notice"] = notice_after_switch and not state["acknowledged"]

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    monkeypatch.setattr(bridge, "_run_tmux", send)
    assert bridge.set_permission_mode(bridge_dir, mode="auto", timeout_s=5.0) == "auto"
    assert keys.count("BTab") == 1
    assert keys.count("Enter") >= 1
    assert keys[0] == ("BTab" if notice_after_switch else "Enter")
