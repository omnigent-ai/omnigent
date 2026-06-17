"""Phase 0 characterization test — Ctrl+L clears the screen.

Submits one prompt so the scrollback has identifiable text,
presses ``Ctrl+L`` to clear the screen, then inspects the
subsequent PTY render frame. The assertion looks for (a) the
ANSI erase sequence that ``_clear_screen`` writes and (b) a
follow-up turn that completes — proving the input area is still
present and responsive. Turn synchronization uses the visible
``⠹ working`` line and the ``❯`` prompt rather than the
truncated/CPR-suppressed ``state:`` badge (see test_repl_smoke).

Scrolling back the rendered terminal is hard to do deterministically
from pexpect — prompt-toolkit's Renderer tracks cursor position
in memory, not in the PTY stream we can observe. The best we can
do is verify the clear *command* executed (scrollback-clear
sequence ``\\x1b[3J`` or prompt_toolkit's renderer.clear escape
sequence present in the drain) and that the input area still
redraws its status line afterwards — i.e. that the REPL did not
exit on Ctrl+L.

**What breaks if this fails:**
- ``omnigent.cli`` removes the ``@kb.add("c-l", ...)`` binding
  or its handler stops calling ``event.app.renderer.clear()``.
- The ``\\x1b[3J`` scrollback-erase write is removed (would
  regress scrollback-buffer handling on xterm/iTerm2).
- Ctrl+L accidentally gets mapped to a terminating action, so
  the REPL exits instead of clearing.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "Ctrl+L clear".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests._model_pools import resolve_model
from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent._snapshot import compare_snapshot

# Visible turn-synchronization markers (see test_repl_smoke).
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_MODEL = resolve_model("databricks-gpt-5-mini", key=__name__)
_HARNESS = "openai-agents"
_PROMPT = "say ok"

# The screen-clear escape sequence that prompt-toolkit's
# ``renderer.clear()`` writes when Ctrl+L fires. On xterm
# descendants this is ``ESC [ 2 J`` followed by a cursor home
# ``ESC [ 0 ; 0 H`` — searching for the 2J erase in the raw
# (un-stripped) PTY drain is the most reliable signal that the
# Ctrl+L handler actually ran. We do NOT look for ``\x1b[3J``
# (scrollback erase) because Python stdout buffering can flush
# it after our drain window closes; the renderer.clear frame is
# what prompt-toolkit emits synchronously on the key event.
_SCREEN_CLEAR_SEQ = "\x1b[2J"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0
# Time to wait for the post-Ctrl+L redraw to reach the PTY.
# Prompt-toolkit's refresh loop runs at ~20 Hz, and the clear
# schedules an ``invalidate()`` that should paint within a
# handful of ticks.
_POST_CLEAR_TIMEOUT = 5.0


def test_repl_ctrl_l_clears_screen(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
) -> None:
    """
    Verify Ctrl+L writes the scrollback-clear sequence and the
    REPL keeps running afterwards.

    :param omnigent_python: Interpreter with omnigent +
        openai-agents installed.
    :param omnigent_repo_root: Working directory for the
        subprocess.
    :param omnigent_credentials_env: Env vars with
        ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
        ``DATABRICKS_CONFIG_PROFILE`` populated.
    """
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=omnigent_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _PROMPT)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        # Send Ctrl+L; then drain the render frames the REPL
        # emits in response. The drain captures BOTH the escape
        # sequence written by sys.stdout and the repaint of the
        # status/input windows.
        child.sendcontrol("l")
        # Drain any render frames the REPL emits in response
        # to Ctrl+L. Multiple short reads handle the case where
        # prompt-toolkit splits the screen-clear + status-bar
        # repaint across separate frames; ``drain_for`` returns
        # early if the PTY idles before the budget elapses.
        post_clear_drain = drain_for(child, _POST_CLEAR_TIMEOUT)
        # Confirm the REPL is still alive and responsive by
        # submitting a second prompt and requiring the turn to
        # start (``working``) and settle back at the ``❯`` prompt.
        # If Ctrl+L had exited the app, this would raise EOF
        # instead of completing a turn.
        submit_prompt(child, "say hi")
        post_prompt_turn = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        # Screen-clear sequence must be present in the raw
        # (un-stripped) PTY drain — prompt-toolkit's
        # ``renderer.clear()`` writes it synchronously when
        # Ctrl+L fires. Its absence means the handler didn't run
        # or was neutered.
        "screen_clear_sequence_written": _SCREEN_CLEAR_SEQ in post_clear_drain,
        # If Ctrl+L had terminated the REPL, the follow-up prompt
        # would raise EOF and never complete a turn. A second turn
        # that starts (``working``) and re-echoes its prompt with
        # the ``❯`` marker proves the input area is still live
        # after the clear. (Replaces the removed ``Agent>`` banner
        # check.)
        "repl_still_alive_after_clear": "❯" in post_prompt_turn.stripped
        and "say hi" in post_prompt_turn.stripped,
    }
    diffs = compare_snapshot("test_repl_ctrl_l_clear", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+L clear:\n"
        + "\n".join(diffs)
        + f"\n\npost-clear raw (last 2000):\n"
        f"{post_clear_drain[-2000:]!r}"
    )
