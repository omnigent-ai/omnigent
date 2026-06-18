"""Phase 0 characterization test — Up-arrow history recall.

Submits two distinguishable prompts, then presses the Up arrow
and asserts the previous prompt's text repopulates the input
area. The assertion looks for the recalled prompt in the
rendered PTY buffer *after* Up is pressed — prompt-toolkit
paints the input window with the recalled entry on the next
render tick.

**What breaks if this fails:**
- ``omnigent.cli`` removes the ``@kb.add("up", ...)`` binding
  that triggers ``history_backward`` on the input buffer.
- ``enable_history_search`` is turned off or the
  ``_sync_input_buffer_history`` path stops appending submitted
  prompts to the history, so Up-arrow cycles to nothing.
- The prompt-toolkit buffer's rendering regresses so the
  recalled text is stored in the buffer but not redrawn in the
  input window.

Turn synchronization uses the visible ``⠹ working`` activity
line and the ``❯`` input prompt rather than the bottom-right
``state:`` badge, which is truncated/CPR-suppressed under a PTY
(see ``test_repl_smoke`` for the full rationale).

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "History recall".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests._model_pools import resolve_model
from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent._snapshot import compare_snapshot

# Visible turn-synchronization markers (see test_repl_smoke).
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_MODEL = resolve_model("databricks-gpt-5-mini", key=__name__)
_HARNESS = "openai-agents"

# Two distinct prompts whose text can be unambiguously recognized
# in the rendered PTY buffer. Short prompts keep the turn latency
# low.
_PROMPT_ONE = "hello-marker-alpha"
_PROMPT_TWO = "hello-marker-beta"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0

# How long to wait for the Up-arrow redraw to paint the recalled
# entry. The refresh loop runs at ~20 Hz (see cli.py); 3 seconds
# is many refresh cycles — a failure here means the keystroke
# wasn't accepted, not that we were impatient.
_RECALL_TIMEOUT = 3.0


def test_repl_history_recall_up_arrow(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
) -> None:
    """
    Submit two prompts, press Up, and verify the most-recent one
    repopulates the input area.

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
        submit_prompt(child, _PROMPT_ONE)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        submit_prompt(child, _PROMPT_TWO)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        # REPL is now back at the idle ``❯`` prompt with an empty
        # input buffer. Press Up to recall _PROMPT_TWO into the
        # input area, then wait for a redraw cycle so the new
        # input-area frame is flushed to the PTY.
        child.send("\x1b[A")  # ANSI escape for Up arrow
        # Drain any buffered render output until the stream
        # idles — drain_for collects whatever prompt-toolkit
        # painted in response to the keystroke across however
        # many render frames it spans.
        recall_drain = drain_for(child, _RECALL_TIMEOUT)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    combined_stripped = strip_ansi(recall_drain) + "\n" + strip_ansi(child.before or "")

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        # The most-recently-submitted prompt must land in the
        # input area. Its presence in the post-Up render frame
        # proves history_backward fired and the buffer repainted.
        "recalled_prompt_in_input_area": _PROMPT_TWO in combined_stripped,
    }
    diffs = compare_snapshot("test_repl_history_recall", observed)
    assert diffs == [], (
        "Snapshot mismatch for history recall (Up arrow):\n"
        + "\n".join(diffs)
        + f"\n\nrecall-drain stripped (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
