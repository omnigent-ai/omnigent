"""Whole-draft clearing and failure boundaries in the native Claude composer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import inject_user_message
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _EFFORT_DIALOG_PANE,
    _picker_bridge_dir,
    _VirtualClock,
)
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _trust_tmp_bridge_parent as _trust_tmp_bridge_parent,
)


class _EditableComposer:
    """Claude Code's input box, editing a multi-line draft as the real one does.

    Measured on Claude Code 2.1.283 and 2.1.287: Ctrl+K deletes to the end of
    the line, or the line break when the cursor is already there; Ctrl+U does
    the same towards the start; Ctrl+A moves to the start of the line. Each
    line of the draft renders as its own row of the box, which shows at most
    a window of a tall draft (seven rows in a 24-row terminal).
    """

    _RULE = "──────────────────────────────"

    def __init__(self, draft: str, *, cursor: int, visible_rows: int | None = None) -> None:
        """
        :param draft: Text the person left in the box.
        :param cursor: Cursor offset into *draft*.
        :param visible_rows: Rows the box shows at once; ``None`` shows the
            whole draft. A taller draft shows its last rows, as the real box
            does with the cursor on the last line.
        """
        self.text = draft
        self.cursor = cursor
        self.visible_rows = visible_rows
        self.submitted: list[str] = []
        self._paste_buffer = ""
        self._actions = {
            "\x1b[107;5u": self._kill_to_line_end,
            "\x1b[117;5u": self._kill_to_line_start,
            "\x1b[97;5u": self._move_to_line_start,
        }

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route the bridge's tmux calls to this box."""
        monkeypatch.setattr(claude_native_bridge, "_run_tmux", self._run_tmux)
        monkeypatch.setattr(claude_native_bridge, "_capture_pane", self._capture_pane)
        monkeypatch.setattr(claude_native_bridge, "time", _VirtualClock())

    def _run_tmux(self, socket_path: str, *args: str) -> None:
        del socket_path
        if args[0] == "load-buffer":
            self._paste_buffer = Path(args[-1]).read_bytes().decode().replace("\r", "\n")
        elif args[0] == "paste-buffer":
            self._insert(self._paste_buffer)
        elif args[0] == "send-keys" and "-l" in args:
            keys = args[-1]
            while keys:
                sequence = next((seq for seq in self._actions if keys.startswith(seq)), None)
                if sequence is None:
                    self._insert(keys)
                    break
                self._actions[sequence]()
                keys = keys[len(sequence) :]
        elif args[0] == "send-keys" and args[-1] == "Enter":
            self.submitted.append(self.text)
            self.text, self.cursor = "", 0

    def _capture_pane(self, socket_path: str, tmux_target: str) -> str:
        del socket_path, tmux_target
        lines = self.text.split("\n")
        if self.visible_rows is not None:
            lines = lines[-self.visible_rows :]
        first, *rest = lines
        rows = "\n".join([f"❯ {first}", *(f"  {row}" for row in rest)])
        return f"{self._RULE}\n{rows}\n{self._RULE}\n  ? for shortcuts\n"

    def _insert(self, text: str) -> None:
        self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
        self.cursor += len(text)

    def _kill_to_line_end(self) -> None:
        if self.text[self.cursor : self.cursor + 1] == "\n":
            end = self.cursor + 1
        else:
            end = self.text.find("\n", self.cursor)
            end = len(self.text) if end == -1 else end
        self.text = self.text[: self.cursor] + self.text[end:]

    def _kill_to_line_start(self) -> None:
        if self.text[self.cursor - 1 : self.cursor] == "\n":
            start = self.cursor - 1
        else:
            start = self.text.rfind("\n", 0, self.cursor) + 1
        self.text = self.text[:start] + self.text[self.cursor :]
        self.cursor = start

    def _move_to_line_start(self) -> None:
        self.cursor = self.text.rfind("\n", 0, self.cursor) + 1


_STALE_MULTILINE_DRAFT = "STALE ONE\nSTALE TWO\nSTALE THREE"


def _inject_message(bridge_dir: Path) -> None:
    inject_user_message(bridge_dir, content="hello from the web")


def _inject_effort(bridge_dir: Path) -> None:
    claude_native_bridge.inject_slash_command(bridge_dir, command="/effort high")


@pytest.mark.parametrize(
    ("inject", "submitted"),
    [(_inject_message, "hello from the web"), (_inject_effort, "/effort high")],
    ids=["message", "slash-command"],
)
def test_injection_clears_every_line_of_a_leftover_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inject: Callable[[Path], None],
    submitted: str,
) -> None:
    """
    A multi-line draft left in the box is not submitted with the injected text.

    Clearing only the cursor's line leaves the draft's other lines in the
    box, and the paste then lands among them: Enter sends the person's
    half-written draft as part of the web message, or sends the draft plus
    ``/effort high`` to the model as a prompt instead of applying the setting.
    """
    bridge_dir = _picker_bridge_dir(tmp_path)
    composer = _EditableComposer(_STALE_MULTILINE_DRAFT, cursor=3)
    composer.install(monkeypatch)

    inject(bridge_dir)

    assert [text.strip() for text in composer.submitted] == [submitted]


_TALL_DRAFT = "\n".join(f"line {index:02d}" for index in range(1, 31))


def test_inject_user_message_clears_a_draft_taller_than_the_input_box(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing repeats until the draft lines above the visible window are removed."""
    bridge_dir = _picker_bridge_dir(tmp_path)
    composer = _EditableComposer(_TALL_DRAFT, cursor=len(_TALL_DRAFT), visible_rows=7)
    composer.install(monkeypatch)

    inject_user_message(bridge_dir, content="hello from the web")

    assert [text.strip() for text in composer.submitted] == ["hello from the web"]


@pytest.mark.parametrize(
    "draft",
    [
        "\n".join(f"line {index:03d}" for index in range(1, 701)),
        "\n".join(["same line"] * 300),
    ],
    ids=["numbered-lines", "identical-lines"],
)
def test_inject_user_message_keeps_clearing_while_a_tall_draft_shrinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    draft: str,
) -> None:
    """
    A draft needing many rounds clears while each round shrinks it.

    Numbered lines change the visible window every round, so clearing may
    outlast the stuck-box budget. Identical lines leave the window looking
    the same, so only the budget itself covers them: about 460 identical
    lines in a seven-row window before the clear fails closed.
    """
    bridge_dir = _picker_bridge_dir(tmp_path)
    composer = _EditableComposer(draft, cursor=len(draft), visible_rows=7)
    composer.install(monkeypatch)

    inject_user_message(bridge_dir, content="hello from the web")

    assert [text.strip() for text in composer.submitted] == ["hello from the web"]
    if draft.startswith("line "):
        assert (
            claude_native_bridge.time.monotonic() > claude_native_bridge._COMPOSER_CLEAR_TIMEOUT_S
        )


class _StuckComposer(_EditableComposer):
    """A box the clearing keys never change."""

    def _kill_to_line_end(self) -> None:
        pass

    def _kill_to_line_start(self) -> None:
        pass


class _ReplacedAfterBurstComposer(_EditableComposer):
    """A box that a clearing burst leaves in another state: shell mode or a dialog."""

    def __init__(self, draft: str, *, cursor: int, replacement: str) -> None:
        super().__init__(draft, cursor=cursor)
        self.replacement = replacement
        self.burst = False

    def _run_tmux(self, socket_path: str, *args: str) -> None:
        if args[0] == "send-keys" and "\x1b[107;5u" in args[-1]:
            self.burst = True
        super()._run_tmux(socket_path, *args)

    def _capture_pane(self, socket_path: str, tmux_target: str) -> str:
        pane = super()._capture_pane(socket_path, tmux_target)
        if not self.burst:
            return pane
        if self.replacement == "shell-mode":
            return pane.replace("❯", "!", 1)
        return _EFFORT_DIALOG_PANE


@pytest.mark.parametrize(
    "inject", [_inject_message, _inject_effort], ids=["message", "slash-command"]
)
@pytest.mark.parametrize(
    ("replacement", "reason"),
    [("shell-mode", "shell mode"), ("dialog", "could not be read")],
    ids=["shell-mode", "dialog"],
)
def test_a_box_replaced_during_clearing_sends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inject: Callable[[Path], None],
    replacement: str,
    reason: str,
) -> None:
    """A box that turns into shell mode or a dialog mid-clear fails before any paste."""
    bridge_dir = _picker_bridge_dir(tmp_path)
    composer = _ReplacedAfterBurstComposer(
        _STALE_MULTILINE_DRAFT, cursor=3, replacement=replacement
    )
    composer.install(monkeypatch)

    with pytest.raises(claude_native_bridge.ClaudeTerminalDialog, match=reason):
        inject(bridge_dir)

    assert composer.submitted == []
    assert "hello" not in composer.text and "/effort" not in composer.text


class _ChurningComposer(_StuckComposer):
    """A box whose text changes on every capture but never empties."""

    captures = 0

    def _capture_pane(self, socket_path: str, tmux_target: str) -> str:
        self.captures += 1
        self.text = f"stuck {self.captures}"
        return super()._capture_pane(socket_path, tmux_target)


def test_clearing_a_box_that_keeps_changing_fails_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Progress extends the budget only up to the overall cap."""
    composer = _ChurningComposer("stuck", cursor=0)
    composer.install(monkeypatch)

    with pytest.raises(claude_native_bridge.ClaudeTerminalDialog, match="could not be cleared"):
        claude_native_bridge._clear_composer("/tmp/example/tmux.sock", "claude:0.0")

    cap = claude_native_bridge._COMPOSER_CLEAR_MAX_S
    assert cap <= claude_native_bridge.time.monotonic() < cap + 1


def test_clearing_a_box_that_never_empties_fails_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A box the clearing keys do not shrink fails the injection once the budget runs out."""
    composer = _StuckComposer("stuck\nstuck", cursor=0)
    composer.install(monkeypatch)
    rounds: list[str] = []
    run_tmux = composer._run_tmux

    def counting_run_tmux(socket_path: str, *args: str) -> None:
        rounds.append(args[-1])
        run_tmux(socket_path, *args)

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", counting_run_tmux)

    with pytest.raises(claude_native_bridge.ClaudeTerminalDialog, match="could not be cleared"):
        claude_native_bridge._clear_composer("/tmp/example/tmux.sock", "claude:0.0")

    budget = claude_native_bridge._COMPOSER_CLEAR_TIMEOUT_S
    assert claude_native_bridge.time.monotonic() >= budget
    # One round per poll interval until the budget runs out.
    assert 2 <= len(rounds) <= budget / claude_native_bridge._CLAUDE_READY_POLL_INTERVAL_S + 1


@pytest.mark.parametrize(
    "inject", [_inject_message, _inject_effort], ids=["message", "slash-command"]
)
@pytest.mark.parametrize("draft", ["stuck", "stuck\nstuck"], ids=["one-row", "two-rows"])
def test_an_unsuccessful_clear_sends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inject: Callable[[Path], None],
    draft: str,
) -> None:
    """Neither injection path pastes or submits while a draft it could not clear remains."""
    bridge_dir = _picker_bridge_dir(tmp_path)
    composer = _StuckComposer(draft, cursor=0)
    composer.install(monkeypatch)

    with pytest.raises(claude_native_bridge.ClaudeTerminalDialog):
        inject(bridge_dir)

    assert composer.submitted == []
    assert composer.text == draft


class _TornCaptureComposer(_EditableComposer):
    """A box whose next captures come back as torn, empty frames."""

    def __init__(self, draft: str, *, cursor: int, torn_frames: int) -> None:
        super().__init__(draft, cursor=cursor)
        self.torn_frames = torn_frames

    def _capture_pane(self, socket_path: str, tmux_target: str) -> str:
        if self.torn_frames:
            self.torn_frames -= 1
            return ""
        return super()._capture_pane(socket_path, tmux_target)


def test_clearing_re_polls_a_torn_capture_instead_of_reading_it_as_no_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty capture says nothing about the draft, so the whole draft still goes."""
    composer = _TornCaptureComposer(_STALE_MULTILINE_DRAFT, cursor=3, torn_frames=2)
    composer.install(monkeypatch)

    claude_native_bridge._clear_composer("/tmp/example/tmux.sock", "claude:0.0")

    assert composer.text == ""


@pytest.mark.parametrize(
    ("pane", "rows"),
    [
        (
            "──────\n❯ one\n  two\n\n  four\n──────\n  ? for shortcuts\n",
            ["❯ one", "  two", "", "  four"],
        ),
        ("──────\n\n❯ one\n  two\n──────\n", ["❯ one", "  two"]),
        ("scrollback\n──────\n❯ one\n  two\n  three", ["❯ one", "  two", "  three"]),
        ("──────\n  Change effort level?\n  ❯ 1. Yes, switch to low\n    2. No, go back\n", None),
        ("plain output, no box\n", None),
    ],
    ids=[
        "framed-with-blank-row",
        "blank-line-under-rule",
        "closing-rule-off-screen",
        "dialog-selector",
        "no-box",
    ],
)
def test_composer_box_rows_reads_every_row_of_the_input_box(
    pane: str, rows: list[str] | None
) -> None:
    """Every row of the live box counts, including a draft's blank lines."""
    assert claude_native_bridge._composer_box_rows(pane) == rows
