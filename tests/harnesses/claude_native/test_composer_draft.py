"""Claude composer frames identify live drafts without touching accepted work."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    _claude_prompt_rendered,
    inject_user_message,
    write_tmux_target,
)


@pytest.fixture(autouse=True)
def _isolated_bridge_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path)


_SUBMITTED_ECHO = """\
❯ Task: ship the paste submit fix and verify
  Task: ship the paste submit fix and verify
"""

_PERMISSION_PROMPT_PANE = """\
╭──────────────────────────────────────────────────────────╮
│ Bash command                                             │
│                                                          │
│ rm -rf build                                             │
│                                                          │
│ Do you want to proceed?                                  │
│ ❯ 1. Yes                                                 │
│   2. Yes, and don't ask again for rm commands in ~/repo   │
│   3. No, and tell Claude what to do differently (esc)     │
╰──────────────────────────────────────────────────────────╯
"""


def test_claude_prompt_rendered_sees_prompt_under_truncated_label() -> None:
    """A clipped title can consume every leading glyph of the opening rule."""
    rule = "─" * 103
    pane = "\n".join(
        [
            '● Agent "fix the webhook retry path" finished · 3m 48s',
            " Retry failed billing webhook deliveries with exponential backoff and"
            " jitter, capped at one hour(… ─",  # opening rule: no leading run
            "❯",
            rule,  # closing rule
            "  Opus 4.8 (1M) │ xhigh │ 237.7k/1M $4.64",
            "  ⏵⏵ auto mode on · 1 shell",
        ]
    )
    assert _claude_prompt_rendered(pane) is True


def test_claude_prompt_rendered_ignores_draft_line_trailing_off_in_a_glyph() -> None:
    """Indented draft content cannot replace the composer's opening rule."""
    rule = "─" * 40
    pane = "\n".join(
        [
            rule,  # opening rule
            "❯ first line of the draft",
            "  second line of the draft trails off… ─",
            "  third line",
            rule,  # closing rule
            "  Opus 4.8 (1M) │ xhigh │ 237.7k/1M $4.64",
        ]
    )
    assert _claude_prompt_rendered(pane) is True


def _composer_pane(draft: str = "") -> str:
    """
    Render a pane whose live input box holds *draft*.

    Claude Code always frames the composer with corner-free rules, and
    that frame is how the bridge tells the live input box apart from a
    prompt echoed into scrollback or an overlay's selected row, so a fake
    pane has to carry it to stand in for the real one.

    :param draft: Text sitting in the composer; empty means idle.
    :returns: The pane text.
    """
    return f"""\
──────────────────────────────
❯ {draft}
──────────────────────────────
  ? for shortcuts
"""


@pytest.mark.parametrize(
    "draft_rows",
    [
        pytest.param("  [Pasted text #1 +7 lines]\n", id="collapsed-placeholder"),
        pytest.param("  Task: ship the paste\n  submit fix and verify\n", id="wrapped-literal"),
        pytest.param("  Task: ship the pa\n  ste submit fix\n", id="wrapped-mid-word"),
    ],
)
def test_inject_user_message_retries_enter_for_a_draft_below_the_prompt_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    draft_rows: str,
) -> None:
    """
    A draft rendered under a blank ``❯`` is still verified and resubmitted.

    Claude Code can leave the prompt row blank and render the paste on the
    rows below it in the framed input box — collapsed to ``[Pasted text #1
    +N lines]`` or as literal text wrapped at the pane width, which can
    split the needle across rows. Reading only the ``❯`` row never sees
    that draft, so delivery sent one blind Enter and reported success; when
    the TUI swallowed it the first prompt of a sub-agent dispatch sat unsent
    until someone pressed Enter.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge._CLAUDE_READY_POLL_INTERVAL_S", 0.01
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._SUBMIT_RETRY_INTERVAL_S", 0.02)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._PASTE_COMMIT_TIMEOUT_S", 0.2)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    pasted_pane = _composer_pane().replace("❯ \n", "❯\n" + draft_rows)
    tui = {"pane": _composer_pane(), "enters": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Simulate a TUI that renders the paste below ``❯`` and swallows the first Enter.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the simulated pane.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = pasted_pane
        if cmd[-1] == "Enter":
            tui["enters"] += 1
            if tui["enters"] > 1:
                tui["pane"] = _composer_pane()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message(bridge_dir, content="Task: ship the paste submit fix and verify\n" * 8)

    assert tui["pane"] == _composer_pane(), "The draft was left unsent in the input box."
    assert tui["enters"] == 2


@pytest.mark.parametrize(
    "after_submit",
    [
        pytest.param(
            lambda: (
                _SUBMITTED_ECHO
                + _composer_pane().replace(
                    "? for shortcuts", "⏵⏵ auto mode on (shift+tab to cycle)"
                )
            ),
            id="empty-box-under-echo",
        ),
        # Lazy: the dialog pane is defined further down this module.
        pytest.param(lambda: _SUBMITTED_ECHO + _PERMISSION_PROMPT_PANE, id="permission-dialog"),
    ],
)
def test_inject_user_message_sends_no_extra_enter_after_an_accepted_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_submit: Callable[[], str],
) -> None:
    """
    Once the draft leaves the input box, no retry Enter follows.

    Reading the whole framed box must not pick up the submitted message's
    transcript echo above the frame, the mode footer below it, or a boxed
    dialog the started turn opens. Any of those read as a still-pending
    draft would send a retry Enter into the running turn, and on a
    permission dialog that Enter approves the tool.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge._CLAUDE_READY_POLL_INTERVAL_S", 0.01
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._SUBMIT_RETRY_INTERVAL_S", 0.02)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    tui = {"pane": _composer_pane(), "enters": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Simulate a TUI that accepts the first Enter.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the simulated pane.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = _composer_pane().replace(
                "❯ \n", "❯\n  Task: ship the paste\n  submit fix and verify\n"
            )
        if cmd[-1] == "Enter":
            tui["enters"] += 1
            tui["pane"] = after_submit()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message(bridge_dir, content="Task: ship the paste submit fix and verify\n" * 8)

    assert tui["enters"] == 1, f"Expected one submit Enter, got {tui['enters']}."


def test_inject_user_message_retries_enter_for_a_draft_with_invisible_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A draft holding U+FEFF is still recognised, so its swallowed Enter is retried.

    Claude Code keeps Unicode format characters in the draft but never
    renders them, and its first Enter only strips them ("Removed 2
    invisible characters · review and press Enter to send"). A needle that
    kept them never matched the pane, so delivery treated the message as
    sent after that one Enter and it sat unsent.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge._CLAUDE_READY_POLL_INTERVAL_S", 0.01
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._SUBMIT_RETRY_INTERVAL_S", 0.02)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._PASTE_COMMIT_TIMEOUT_S", 0.2)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    tui = {"pane": _composer_pane(), "enters": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Simulate a TUI that hides U+FEFF and spends the first Enter stripping it.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the simulated pane.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = _composer_pane("Task: ship the fix")
        if cmd[-1] == "Enter":
            tui["enters"] += 1
            if tui["enters"] == 1:
                tui["pane"] = _composer_pane("Task: ship the fix").replace(
                    "? for shortcuts",
                    "Removed 2 invisible characters · review and press Enter to send",
                )
            else:
                tui["pane"] = _composer_pane()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message(bridge_dir, content="\ufeff\ufeffTask: ship the fix")

    assert tui["pane"] == _composer_pane(), "The draft was left unsent in the input box."
    assert tui["enters"] == 2


def test_inject_user_message_ignores_rows_below_an_unclosed_input_box(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no closing rule under the prompt row, the rows below are not read as the draft.

    Here the started turn's pane shows the empty ``❯`` row directly under
    the last rule, with the mode footer and a status line carrying the
    session title (the prompt's first words) below it. Counting those rows
    as the input box would read the title as a still-pending draft and
    send retry Enters into the running turn.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge._CLAUDE_READY_POLL_INTERVAL_S", 0.01
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._SUBMIT_RETRY_INTERVAL_S", 0.02)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._SUBMIT_VERIFY_TIMEOUT_S", 0.5)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    unclosed_after_submit = """\
❯ Task: ship the paste submit fix
──────────────────────────────
❯
  ⏵⏵ auto mode on (shift+tab to cycle)
  Task: ship the paste submit fix · Opus
"""
    tui = {"pane": _composer_pane(), "enters": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Simulate a TUI that accepts the first Enter and leaves the box unclosed.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the simulated pane.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = _composer_pane("Task: ship the paste submit fix")
        if cmd[-1] == "Enter":
            tui["enters"] += 1
            tui["pane"] = unclosed_after_submit
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message(bridge_dir, content="Task: ship the paste submit fix")

    assert tui["enters"] == 1, f"Expected one submit Enter, got {tui['enters']}."


_PLACEHOLDER_BELOW_GLYPH_PANE = """\
──────────────────────────────
❯
  [Pasted text #1 +3 lines]
──────────────────────────────
  ? for shortcuts
"""

_RUNNING_TURN_WITH_ECHO_PANE = """\
❯ what happened?
● Looking at the worktree…
✻ Baked for 4s
──────────────────────────────
❯
──────────────────────────────
  ? for shortcuts
"""


def test_draft_in_input_box_sees_a_placeholder_on_the_row_below_the_glyph() -> None:
    """
    The observed wedge: Claude Code renders a paste that landed with a stray
    leading newline as an EMPTY glyph row with the ``[Pasted text #1 +N
    lines]`` placeholder on the row below. Reading only the glyph row said
    "no draft", so a swallowed Enter was never retried and the worker sat
    idle forever.
    """
    assert claude_native_bridge._draft_in_input_box(_PLACEHOLDER_BELOW_GLYPH_PANE, "ignored")


def test_draft_in_input_box_ignores_a_transcript_echo_outside_the_frame() -> None:
    """A submitted message's echo above the box is not the draft — only the framed box counts."""
    assert not claude_native_bridge._draft_in_input_box(
        _RUNNING_TURN_WITH_ECHO_PANE, "what happened?"
    )
    assert claude_native_bridge._draft_in_input_box(
        _composer_pane("what happened?"), "what happened?"
    )


_SHORTCUTS_PANEL_WITH_DRAFT_PANE = """\
──────────────────────────────
❯ fix the flaky test
──────────────────────────────
  ! for shell mode        double tap esc to clear input
  / for commands          shift + tab to auto-accept edits
"""

_SHORTCUTS_PANEL_WITH_STRANDED_PLACEHOLDER_PANE = """\
──────────────────────────────
❯
  [Pasted text #1 +3 lines]
──────────────────────────────
  ! for shell mode        double tap esc to clear input
  / for commands          shift + tab to auto-accept edits
"""


def test_draft_in_input_box_reads_the_composer_above_the_shortcuts_panel() -> None:
    """
    With the ``?`` shortcuts panel expanded, its "! for shell mode" row sits
    directly under the box's closing rule and starts with the shell-mode
    glyph. The composer region must stay the framed box above it, so a draft
    still sitting there — verbatim or stranded as the placeholder below an
    empty glyph row — keeps reading as present.
    """
    assert claude_native_bridge._draft_in_input_box(
        _SHORTCUTS_PANEL_WITH_DRAFT_PANE, "fix the flaky test"
    )
    assert claude_native_bridge._draft_in_input_box(
        _SHORTCUTS_PANEL_WITH_STRANDED_PLACEHOLDER_PANE, "ignored"
    )
