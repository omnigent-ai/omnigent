"""Phase 0 characterization test — REPL smoke under pexpect.

Spawns ``omnigent run <yaml>`` under a PTY, waits for the
REPL's ``❯`` input prompt, types a prompt, awaits the turn
completion, then exits cleanly via Ctrl+D. Proves the REPL's
basic input/output pipeline works end-to-end. The full
key-binding / overview / interrupt matrix lands in follow-ups
per the design doc.

Synchronization contract (post REPL UI refactor): the turn is
driven off the visible ``⠹ working`` activity line and the
``❯`` input prompt that re-renders when the turn settles —
the same markers the green Ctrl+R / ``/model`` e2e tests use.
The old bottom-right ``state: running``/``state: sleeping``
badge is no longer a reliable pexpect signal: it sits at the
far edge of the toolbar and is truncated/CPR-suppressed under
a PTY. Likewise the user/assistant turns now render as a ``❯``
echo and a ``◆ <model>`` header — the legacy ``You>`` / ``Agent>``
banners were removed in the prompt-toolkit rewrite.

**What breaks if this fails:**
- ``omnigent.cli._run_agent`` REPL entrypoint stops booting
  under a PTY (prompt-toolkit layout errors, terminal-type
  handling regression).
- The REPL stops echoing the submitted prompt with its ``❯``
  marker — the submission never reached the Session.
- ``Session.stream_turn`` stops actually producing events
  when driven from the REPL (turn never returns to the idle
  ``❯`` prompt).
- Ctrl+D stops terminating the REPL cleanly (either hangs the
  subprocess or crashes on shutdown).

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite; smoke tier only for v1.
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
from tests.e2e.omnigent._snapshot import compare_snapshot

# openai-agents is used here for the same reason as the serve
# test — no ``~/.databrickscfg`` patching required.
_MODEL = resolve_model("databricks-gpt-5-mini", key=__name__)
_HARNESS = "openai-agents"
_PROMPT = "say hi in 5 words"

# Visible turn-synchronization markers (see module docstring).
# ``working`` is the activity-line label the REPL paints while a
# turn streams; ``❯ `` is the input prompt it re-renders when the
# turn settles. Both are mid-toolbar / mid-screen text that
# survives PTY truncation, unlike the far-right ``state:`` badge.
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

# Minimum stripped-length for the turn's rendered output. The
# REPL always echoes the prompt, the ``❯`` marker, spinner
# frames, and a status line — even a failed turn produces more
# than a few dozen characters. Setting this high enough that a
# blank turn fails, low enough that a terse reply passes.
_MIN_STRIPPED_TURN_CHARS = 100

# Timeouts (seconds). The spawn-level default is used by all
# expect calls unless overridden by the harness helpers. The
# individual limits here are tuned to the phases they cover:
# boot must be fast, running transition must be fast, turn
# completion tolerates full LLM latency, shutdown must be quick
# (anything slower than ~10s indicates a Ctrl+D regression).
_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def test_repl_smoke_single_prompt(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
) -> None:
    """
    Spawn the REPL, type one prompt, assert assistant text
    renders, and exit cleanly on Ctrl+D.

    Uses the ``_pexpect_harness`` helpers so the synchronization
    details (state-bar expects, send-then-CR submission) live
    in one place and this test reads as a narrative.

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
        # Boot readiness: the visible ``❯`` input prompt. Prefer it
        # over the bottom-toolbar ``state:`` badge, which CPR
        # suppression can hide under a PTY even when the REPL is ready.
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _PROMPT)
        turn = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        # Drain anything rendered between the settled ``❯`` prompt
        # and Ctrl+D — the post-turn render often repaints the
        # assistant text outside the range await_turn_complete
        # captured. We check both the captured turn and the
        # post-drain buffer.
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
        signal_status = child.signalstatus
    finally:
        if not child.closed:
            child.close(force=True)

    # Combine the in-turn stripped buffer with the final
    # before-buffer so the assertion survives whichever render
    # frame the assistant text happens to live in.
    combined_stripped = turn.stripped + "\n" + strip_ansi(child.before or "")

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        "signal_status": signal_status,
        # The ``❯`` marker is the REPL's deterministic echo of the
        # user's submitted prompt (``RichBlockFormatter.user_message``
        # renders ``❯ <text>``); its presence with the prompt text
        # proves the submission reached the Session, not just the
        # input buffer. Replaces the removed ``You>`` banner.
        "user_prompt_echoed": "❯" in combined_stripped and _PROMPT in combined_stripped,
    }
    diffs = compare_snapshot("test_repl_smoke", observed)
    assert diffs == [], (
        "Snapshot mismatch for REPL smoke:\n"
        + "\n".join(diffs)
        + f"\n\nstripped buffer (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
    assert len(turn.stripped) >= _MIN_STRIPPED_TURN_CHARS, (
        f"Turn output shorter than {_MIN_STRIPPED_TURN_CHARS} "
        f"chars after ANSI strip; got {len(turn.stripped)} chars. "
        f"Likely the state-bar expect synchronized on a premature "
        f"sleep→running→sleep oscillation.\n\n"
        f"stripped (last 1000):\n{turn.stripped[-1000:]}"
    )
