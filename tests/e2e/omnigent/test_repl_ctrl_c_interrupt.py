"""Phase 0 characterization test — mid-turn cancellation re-arms the REPL.

Submits a long-running prompt that produces visible streaming
text, then issues the REPL's documented mid-turn cancellation
mechanism. Asserts (a) the REPL stays alive (does NOT exit)
after the cancellation, and (b) a follow-up prompt is accepted
and produces a new assistant response — proving the streaming
consumer re-armed for the next turn instead of getting stuck
in a half-cancelled state.

**The cancel gesture:** the REPL binds ``Escape`` to
``host.cancel()`` (the ``@kb.add("escape")`` handler in
``omnigent_ui_sdk.terminal._host``), which cancels the in-flight
turn and renders a muted ``cancelled`` line — the surface the
bottom toolbar advertises as "Esc cancel". This is the live
mid-turn cancel path, and the design doc's spec ("send the
cancel key, assert the REPL stays alive") maps onto it: the
in-flight turn is cancelled, the REPL stays alive, and the next
prompt re-uses the same streaming consumer. When Ctrl+C is later
re-pointed from ``app.exit`` to this same cancel call, the test
need only swap the keystroke and the assertions still hold.

(The ``/cancel`` slash command is NOT used here: its REPL adapter
``cancel()`` returns ``None`` and prints nothing, so it gives no
observable ack to synchronize on. Escape is the gesture that both
cancels AND renders proof.)

Turn synchronization uses the visible ``⠹ working`` activity
line and the ``❯`` input prompt rather than the bottom-right
``state:`` badge (truncated/CPR-suppressed under a PTY — see
test_repl_smoke), and the cancel acknowledgement is the muted
``cancelled`` line the run loop prints on Escape.

**What breaks if this fails:**
- The Escape cancel binding regresses so the in-flight turn
  doesn't stop (no ``cancelled`` ack would print).
- The REPL's stream consumer fails to re-arm after
  cancellation — would manifest as the follow-up prompt never
  reaching ``working`` or never settling back at ``❯``. **This
  is the regression the design identified as the highest-priority
  interrupt test.**

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "Ctrl+C interrupt mid-stream".
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

# Visible turn-synchronization markers (see test_repl_smoke).
# ``working`` is the streaming activity line; ``❯ `` is the idle
# input prompt. Both survive PTY truncation, unlike the far-right
# ``state:`` badge the test originally synchronized on.
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

# openai-agents top-level harness — supports turn cancellation
# (supports_turn_cancellation == True for streaming-capable
# harnesses), which the ``/cancel`` slash command requires.
_MODEL = resolve_model("databricks-gpt-5-mini", key=__name__)
_HARNESS = "openai-agents"

# A prompt that produces visibly-long streaming output so the
# cancellation lands while the turn is mid-flight rather than
# right after the assistant finishes. Counting forces many
# tokens; "slowly" nudges the model toward verbose, evenly-
# paced output.
_LONG_PROMPT = (
    "Count slowly from 1 to 100. Print one number per line, "
    "with a short verbal description after each number "
    "explaining what the number could mean. Take your time."
)
_FOLLOW_UP_PROMPT = "say hi"

# Cancellation acknowledgement rendered by the REPL's run loop
# when the in-flight turn is cancelled: the muted ``cancelled``
# line emitted right after ``session.cancel()`` in the Escape
# handler path (repl/_repl.py). Matching it proves the cancel
# gesture actually interrupted the streaming turn rather than
# being silently dropped.
_CANCEL_ACK_MARKER = r"cancelled"

# The ``◆`` diamond the formatter commits in front of an assistant
# message (``_DiamondMarkdown`` in omnigent_ui_sdk; ``◆ <model>`` on
# the resume path). It is committed to scrollback only when the model
# actually returns text, and never appears in the user-prompt echo
# (``❯``) or toolbar chrome — so it is an assistant-ONLY signal, not
# satisfiable by the submitted prompt's echo.
_ASSISTANT_HEADER_GLYPH = "◆"

# Minimum prose length (after the ``◆`` header) required to count the
# follow-up as a real assistant response. A bare header with no body
# — or the prompt echo alone — must not pass. Two chars clears those
# while staying robust to a terse reply like "Hi".
_MIN_ASSISTANT_BODY_CHARS = 2

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
# Initial turn must be long-lived enough for cancellation to
# land mid-stream. Setting a generous ceiling lets a slow LLM
# still hit the cancel path; if the turn finishes too fast we
# still verify cancellation was attempted via the status line.
_INITIAL_RUNNING_BUDGET = 30.0
_CANCEL_ACK_TIMEOUT = 30.0
_FOLLOWUP_RUNNING_TIMEOUT = 30.0
_FOLLOWUP_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def test_repl_cancel_re_arms_for_next_turn(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
) -> None:
    """
    Submit a long prompt, ``/cancel`` it mid-stream, then
    submit a follow-up and verify it completes — proving the
    REPL stayed alive AND the streaming consumer re-armed.

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
        submit_prompt(child, _LONG_PROMPT)
        # Wait for the turn to actually start streaming — the
        # visible ``⠹ working`` activity line marks the moment the
        # executor accepted the prompt and is producing output.
        # Only after this is cancellation meaningful (cancelling a
        # not-yet-running turn would no-op against an idle session).
        child.expect(_RUNNING_MARKER, timeout=_INITIAL_RUNNING_BUDGET)
        # Press Escape — the REPL's live mid-turn cancel gesture
        # (the ``@kb.add("escape")`` binding calls ``host.cancel()``,
        # which cancels the in-flight task and the run loop renders
        # the muted ``cancelled`` line). This is the documented
        # cancel surface the toolbar advertises as "Esc cancel", and
        # the same path the design doc says Ctrl+C will re-point to.
        child.send("\x1b")
        # The muted ``cancelled`` line is the observable proof the
        # gesture actually interrupted the streaming turn. Its
        # absence within the budget would mean the cancel was
        # silently dropped — a failure this test is designed to
        # catch.
        child.expect(_CANCEL_ACK_MARKER, timeout=_CANCEL_ACK_TIMEOUT)
        # Follow-up prompt — proves the input area still accepts
        # text and the streaming consumer re-armed. If the consumer
        # were stuck after cancellation, the follow-up would never
        # reach ``working`` or never settle back at ``❯``.
        submit_prompt(child, _FOLLOW_UP_PROMPT)
        followup_turn = await_turn_complete(
            child,
            running_timeout=_FOLLOWUP_RUNNING_TIMEOUT,
            completion_timeout=_FOLLOWUP_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    # Merge the captured turn with the post-exit before-buffer so the
    # echo assertion survives whichever render frame the short
    # follow-up prompt's ``❯ <text>`` echo happens to land in (the
    # echo paints around the ``working`` handshake boundary).
    combined_stripped = followup_turn.stripped + "\n" + strip_ansi(child.before or "")

    # Assistant-only signal: the ``◆`` diamond header the formatter
    # commits in front of an assistant message (``_DiamondMarkdown``
    # in omnigent_ui_sdk; ``◆ <model>`` on the resume path). It is
    # emitted ONLY when the model actually returns text — a failed or
    # empty turn (e.g. the consumer not re-arming after cancellation)
    # commits no ◆ and no body. Crucially this glyph never appears in
    # the user-prompt echo (``❯ <text>``) or the toolbar chrome, so —
    # unlike a bare non-empty-length check, which the prompt echo
    # alone satisfies — it cannot be faked by the submitted prompt.
    diamond_idx = combined_stripped.find(_ASSISTANT_HEADER_GLYPH)
    # Require real prose after the header, not just a bare diamond, so
    # a phantom header with no body can't pass either.
    assistant_body = (
        combined_stripped[diamond_idx + len(_ASSISTANT_HEADER_GLYPH) :]
        if diamond_idx != -1
        else ""
    )

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        # The follow-up turn must produce an assistant message: the
        # ``◆`` header must be present AND followed by a non-trivial
        # body. This proves the streaming consumer re-armed and the
        # model returned text after the cancellation — the exact
        # regression this test exists to catch. (Replaces the removed
        # ``Agent>`` banner check; a non-empty-length check would be a
        # tautology because the prompt echo is always present.)
        "follow_up_assistant_response_rendered": diamond_idx != -1
        and len(assistant_body.strip()) >= _MIN_ASSISTANT_BODY_CHARS,
        # Follow-up's user-prompt echo must also be present — the
        # ``❯ <text>`` echo proves the input area accepted the second
        # submission (not just the cancellation). (Replaces the
        # removed ``You>`` banner check.)
        "follow_up_user_prompt_echoed": "❯" in combined_stripped
        and _FOLLOW_UP_PROMPT in combined_stripped,
    }
    diffs = compare_snapshot("test_repl_ctrl_c_interrupt", observed)
    assert diffs == [], (
        "Snapshot mismatch for cancellation re-arm:\n"
        + "\n".join(diffs)
        + f"\n\nfollow-up turn + tail stripped (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
