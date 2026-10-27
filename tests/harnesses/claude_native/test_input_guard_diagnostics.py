"""Explain safe input refusals without retaining terminal or prompt contents."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.harnesses.claude_native import bridge, delivery_diagnostics

_PRIVATE_TEXT = "private draft and command text"
_RULE = "──────────────────────────────────────────────────────────────"
_PROMPT = (
    f"{_RULE}\nDo you want to continue?\n  1. Yes\n  2. No\nEnter to select · Esc to cancel\n"
)


def _composer(text: str = "", *, glyph: str = "❯") -> str:
    return f"{_RULE}\n{glyph} {text}\n{_RULE}\n"


class _Terminal:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.directory = tmp_path / "bridge"
        self.pane = _composer()
        self.pasted = False
        self.now = 0.0
        self.commands: list[tuple[str, ...]] = []
        self.newline_on_enter = False
        self.pending_source: str | None = None
        self.pending_after_paste = False
        self.clears_surface = False
        monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
        monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
        monkeypatch.setattr(
            bridge,
            "time",
            SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep, time=lambda: self.now),
        )
        monkeypatch.setattr(delivery_diagnostics, "time", bridge.time)
        monkeypatch.setattr(bridge, "_capture_pane", lambda *_: self.capture())
        monkeypatch.setattr(bridge, "_run_tmux", self.run)
        monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", lambda *_, **__: None)
        monkeypatch.setattr(bridge, "_clear_composer", lambda *_: None)
        monkeypatch.setattr(
            bridge,
            "_has_approval_wait",
            lambda *_: self.pending_source == "hook_wait" and self.pending,
        )
        monkeypatch.setattr(bridge, "_OCCUPIED_INPUT_DISMISS_TIMEOUT_S", 0.08)
        monkeypatch.setattr(bridge, "_OCCUPIED_INPUT_DISMISS_RETRY_INTERVAL_S", 0.02)
        monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0.0)
        monkeypatch.setattr(bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.08)
        monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.02)
        bridge.write_tmux_target(
            self.directory, socket_path=tmp_path / "socket", tmux_target="main"
        )

    @property
    def pending(self) -> bool:
        return not self.pending_after_paste or self.pasted

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def capture(self) -> str:
        if self.pending_source == "visible_prompt" and self.pending:
            return _PROMPT + _PRIVATE_TEXT
        return self.pane

    def run(self, socket: str, *args: str) -> None:
        self.commands.append(args)
        if args[0] == "paste-buffer":
            self.pasted = True
            self.pane = _composer(_PRIVATE_TEXT)
        if args[-1] == "Enter" and self.newline_on_enter:
            self.pane = self.pane.replace(f"\n{_RULE}\n", f"\n\n{_RULE}\n")
        if self.clears_surface and (
            args[-1] == "Escape" or bridge._COMPOSER_KILL_TO_LINE_START in args[-1]
        ):
            self.pane = _composer()


@pytest.fixture
def terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Terminal:
    return _Terminal(tmp_path, monkeypatch)


def _summary(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "claude_native_delivery_finished"
    ]
    assert len(records) == 1
    row = record_to_row(records[0], "harness")
    assert _PRIVATE_TEXT not in json.dumps(row)
    assert _PRIVATE_TEXT not in records[0].getMessage()
    assert delivery_diagnostics._prompt_delivery_trace.get() is None
    return records[0].attributes


@pytest.mark.parametrize("has_draft", [False, True])
def test_shell_refusal_records_recovery_attempts_before_any_paste(
    terminal: _Terminal, caplog: pytest.LogCaptureFixture, has_draft: bool
) -> None:
    terminal.pane = _composer(_PRIVATE_TEXT if has_draft else "", glyph="!")
    with (
        caplog.at_level("INFO", logger=delivery_diagnostics.__name__),
        pytest.raises(bridge.ClaudeTerminalDialog),
    ):
        bridge.inject_user_message(terminal.directory, content=_PRIVATE_TEXT)

    attrs = _summary(caplog)
    assert attrs["attempt"] == 0
    assert attrs["stage"] == "restoring_input"
    assert attrs["restore_first_surface"] == attrs["restore_last_surface"] == "shell_mode"
    assert attrs["restore_captures"] > 2
    assert attrs["restore_pane_rows"] == 3
    assert attrs["restore_capture_empty"] is False
    action = "shell_clear" if has_draft else "escape"
    assert attrs[f"restore_{action}_attempts"] == len(terminal.commands)
    assert attrs[f"restore_{action}_attempts"] > 1
    assert all(command[-1] != "Enter" for command in terminal.commands)
    assert not terminal.pasted


def test_recovered_surface_is_retained_when_a_later_submit_fails(
    terminal: _Terminal, caplog: pytest.LogCaptureFixture
) -> None:
    terminal.pane = _composer(_PRIVATE_TEXT, glyph="!")
    terminal.clears_surface = True
    with (
        caplog.at_level("INFO", logger=delivery_diagnostics.__name__),
        pytest.raises(bridge.ClaudeMessageNotAccepted),
    ):
        bridge.inject_user_message(terminal.directory, content=_PRIVATE_TEXT)

    attrs = _summary(caplog)
    assert attrs["attempt"] == 1
    assert attrs["restore_first_surface"] == "shell_mode"
    assert attrs["restore_last_surface"] == "none"
    assert attrs["restore_shell_clear_attempts"] == 1
    assert attrs["verification"] == "draft_still_present"


@pytest.mark.parametrize("source", ["hook_wait", "visible_prompt"])
@pytest.mark.parametrize("after_paste", [False, True])
def test_pending_prompt_records_source_and_delivery_boundary(
    terminal: _Terminal,
    caplog: pytest.LogCaptureFixture,
    source: str,
    after_paste: bool,
) -> None:
    terminal.pending_source = source
    terminal.pending_after_paste = after_paste
    with (
        caplog.at_level("INFO", logger=delivery_diagnostics.__name__),
        pytest.raises(bridge.ClaudeUserPromptPending),
    ):
        bridge.inject_user_message(terminal.directory, content=_PRIVATE_TEXT)

    attrs = _summary(caplog)
    assert attrs["pending_prompt_source"] == source
    assert attrs["pending_prompt_after_paste"] is after_paste
    assert attrs["pending_prompt_after_submit"] is False
    assert attrs["pending_prompt_stage"] == (
        "checking_pending_prompt" if after_paste else "restoring_input"
    )
    assert terminal.pasted is after_paste
    assert all(command[-1] != "Enter" for command in terminal.commands)


@pytest.mark.parametrize("newline_on_enter", [False, True])
def test_rejected_submit_distinguishes_unchanged_and_growing_composers(
    terminal: _Terminal,
    caplog: pytest.LogCaptureFixture,
    newline_on_enter: bool,
) -> None:
    terminal.newline_on_enter = newline_on_enter
    with (
        caplog.at_level("INFO", logger=delivery_diagnostics.__name__),
        pytest.raises(bridge.ClaudeMessageNotAccepted),
    ):
        bridge.inject_user_message(terminal.directory, content=_PRIVATE_TEXT)

    attrs = _summary(caplog)
    enters = sum(command[-1] == "Enter" for command in terminal.commands)
    assert enters > 1
    assert attrs["submit_composer_initial_rows"] == 1
    assert attrs["submit_composer_initial_blank_rows"] == 0
    assert attrs["submit_composer_last_rows"] == 1 + (enters if newline_on_enter else 0)
    assert attrs["submit_composer_last_blank_rows"] == (enters if newline_on_enter else 0)
    assert attrs["submit_composer_shape_changes"] == (enters if newline_on_enter else 0)
    assert attrs["submit_composer_initial_characters"] == attrs["submit_composer_last_characters"]
    assert attrs["verification"] == "draft_still_present"


def test_composer_observation_failure_preserves_submit_refusal(
    terminal: _Terminal, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original = delivery_diagnostics.record_submit_composer

    def broken_reader(pane: str) -> tuple[list[str], bool] | None:
        raise ValueError(_PRIVATE_TEXT)

    def broken_observation(**kwargs: object) -> None:
        kwargs["box_reader"] = broken_reader
        original(**kwargs)

    monkeypatch.setattr(delivery_diagnostics, "record_submit_composer", broken_observation)
    with (
        caplog.at_level("INFO", logger=delivery_diagnostics.__name__),
        pytest.raises(bridge.ClaudeMessageNotAccepted),
    ):
        bridge.inject_user_message(terminal.directory, content=_PRIVATE_TEXT)

    attrs = _summary(caplog)
    assert attrs["verification"] == "draft_still_present"
    assert "submit_composer_last_rows" not in attrs
