from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge


def _pane(draft: str = "") -> str:
    return f"──────────────────────────────\n❯ {draft}\n──────────────────────────────\n"


class _Delivery:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0.0
        self.pending: list[tuple[float, Callable[[], None]]] = []
        self.directory = tmp_path / "bridge"
        self.transcript = tmp_path / "session.jsonl"
        self.transcript.touch()
        self.pane = _pane()
        self.text = ""
        self.payload = b""
        self.commands: list[tuple[str, ...]] = []
        self.enters: list[float] = []
        self.pasted_at = 0.0
        self.after_paste: Callable[[], None] | None = None
        self.on_enter: Callable[[], None] = self.accept
        monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
        monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setattr(
            bridge,
            "time",
            SimpleNamespace(monotonic=lambda: self.now, time=self.wall_time, sleep=self.sleep),
        )
        monkeypatch.setattr(bridge, "_run_tmux", self.run_tmux)
        monkeypatch.setattr(bridge, "_capture_pane", lambda *_args: self.pane)
        monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.05)
        monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", 3.0)
        monkeypatch.setattr(bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 1.0)
        bridge.write_tmux_target(
            self.directory, socket_path=tmp_path / "tmux.sock", tmux_target="claude:0.0"
        )
        self.announce()

    def wall_time(self) -> float:
        return 1_700_000_000.0 + self.now

    def announce(self) -> None:
        bridge.record_hook_event(
            self.directory,
            {
                "hook_event_name": "SessionStart",
                "session_id": "parent-session",
                "transcript_path": str(self.transcript),
            },
        )

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        due = [action for when, action in self.pending if when <= self.now]
        self.pending = [(when, action) for when, action in self.pending if when > self.now]
        for action in due:
            action()

    def record(self, content: object | None = None, **fields: Any) -> dict[str, Any]:
        return {
            "type": "user",
            "uuid": f"user-{self.now}",
            "sessionId": "parent-session",
            "timestamp": datetime.fromtimestamp(self.wall_time(), UTC).isoformat(),
            "message": {"role": "user", "content": self.text if content is None else content},
            **fields,
        }

    def append(self, entry: dict[str, Any]) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def accept(self) -> None:
        self.append(self.record())
        self.pane = _pane()

    def run_tmux(self, _socket: str, *args: str) -> None:
        self.commands.append(args)
        if args[0] == "load-buffer":
            self.payload = Path(args[-1]).read_bytes()
            self.text = self.payload.decode().replace("\r", "\n").strip()
        if args[0] == "paste-buffer":
            self.pasted_at = self.now
            self.pane = _pane(self.text)
            if self.after_paste is not None:
                self.after_paste()
        if args[-1] == "Enter":
            self.enters.append(self.now)
            self.on_enter()

    def inject(self, text: str = "confirm this user message") -> None:
        bridge.inject_user_message(self.directory, content=text)


@pytest.fixture
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Delivery:
    return _Delivery(tmp_path, monkeypatch)


def test_draft_changes_restart_the_paste_settle_wait(delivery: _Delivery) -> None:
    def finish_paste() -> None:
        delivery.pane = _pane(delivery.text[:-1])
        delivery.pending.append(
            (delivery.now + 1.9, lambda: setattr(delivery, "pane", _pane(delivery.text)))
        )

    delivery.after_paste = finish_paste
    delivery.inject()
    assert delivery.enters[0] - delivery.pasted_at >= 2.2


def test_visible_draft_can_remain_in_the_paste_input_window(delivery: _Delivery) -> None:
    def enter() -> None:
        if delivery.now - delivery.pasted_at < 1.5:
            delivery.pane = _pane(delivery.text + "\n")
        else:
            delivery.accept()

    delivery.on_enter = enter
    delivery.inject()
    assert len(delivery.enters) == 1
    assert len(delivery.transcript.read_text().splitlines()) == 1


def test_paste_input_gap_starts_after_the_paste_command_returns(
    delivery: _Delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = delivery.run_tmux
    completed_at = 0.0

    def delayed_paste(socket: str, *args: str) -> None:
        nonlocal completed_at
        original(socket, *args)
        if args[0] == "paste-buffer":
            delivery.sleep(1.0)
            completed_at = delivery.now

    monkeypatch.setattr(bridge, "_run_tmux", delayed_paste)
    delivery.inject()
    assert delivery.enters[0] - completed_at >= 2.0
    assert len(delivery.enters) == 1


@pytest.mark.parametrize("budget", [3.0, 5.0])
def test_recognized_draft_that_never_settles_stays_unsent(
    delivery: _Delivery, monkeypatch: pytest.MonkeyPatch, budget: float
) -> None:
    monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", budget)
    captures = 0

    def capture(*args: object) -> str:
        nonlocal captures
        if not delivery.text:
            return _pane()
        captures += 1
        pane = _pane(delivery.text + str(captures))
        assert bridge._draft_in_input_box(pane, bridge._submit_needle(delivery.text))
        return pane

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    with pytest.raises(bridge.ClaudeTerminalDialog, match="did not settle"):
        delivery.inject()
    assert delivery.enters == []


def test_unrecognized_paste_keeps_the_existing_fallback(delivery: _Delivery) -> None:
    delivery.after_paste = lambda: setattr(delivery, "pane", _pane("opaque placeholder"))
    delivery.inject()
    assert len(delivery.enters) == 1
    assert delivery.enters[0] - delivery.pasted_at >= 2.0
    assert delivery.payload == bridge._paste_payload_bytes(delivery.text + "\n")


def test_outer_whitespace_reflow_does_not_restart_settlement(delivery: _Delivery) -> None:
    delivery.after_paste = lambda: delivery.pending.append(
        (
            delivery.now + 1.95,
            lambda: setattr(delivery, "pane", _pane("   " + delivery.text + "   ")),
        )
    )
    delivery.inject()
    assert len(delivery.enters) == 1
    assert 2.0 <= delivery.enters[0] - delivery.pasted_at <= 2.1


def test_previously_recognized_draft_does_not_become_a_blind_fallback(
    delivery: _Delivery,
) -> None:
    delivery.after_paste = lambda: delivery.pending.append(
        (delivery.now + 0.05, lambda: setattr(delivery, "pane", _pane("opaque placeholder")))
    )
    with pytest.raises(RuntimeError):
        delivery.inject()
    assert delivery.enters == []
