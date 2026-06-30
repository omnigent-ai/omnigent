"""
End-to-end test for the rate-limit-search policy ASK / approve
flow under ``omnigent run``.

Reproduces the user-reported scenario verbatim:

1. Spawn ``omnigent run examples/rate_limited_search_agent.yaml``
   under a real PTY (Databricks routing via the credentials env).
2. Send a prompt that asks for 4 web searches — the policy ALLOWs
   the first 3 and ASKs on the 4th.
3. Wait for the ``approval required`` banner.
4. Type ``y`` to approve.
5. Verify the REPL echoes ``approved`` (proving ``y`` was
   routed as a verdict, not a message), the approval event route
   returned 202 (no ``POST approval event failed`` warning
   leaked into the buffer), and the 4th tool call actually
   runs (the result panel for ``elephant`` is rendered).

Regression target: an earlier bug had the approval event route
making a synchronous call from inside an async FastAPI handler.
The blocking call raised ``RuntimeError`` from inside the event
loop, which bubbled to the global handler → 500 ``internal_error``
→ SDK logged ``POST elicitation verdict failed`` → parked workflow
only recovered when its 30s ``ask_timeout`` fired, by which point
the verdict had been classified as refused. The user saw a
``[Denied by policy: ...]`` sentinel even though they typed ``y``.

Fix: wrap the sync DBOS / store calls in ``asyncio.to_thread``
so the route handler stays loop-safe (matches the pattern
already used by the PATCH ``/v1/responses`` route in
``responses.py``).

What breaks if this test fails:
  - The route regresses to calling sync DBOS APIs from the
    async handler — every TOOL_CALL ASK approval 500s.
  - The ``asyncio.to_thread`` wrappers are removed.
  - Some path leaks ``POST approval event failed`` into
    the user's buffer (the SDK's warning about a failed
    verdict event).
"""

from __future__ import annotations

import io
from pathlib import Path

from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)

# The agent the user actually runs in their reproducer.
_YAML_REL = "tests/resources/examples/rate_limited_search_agent.yaml"
# Override the YAML's ``databricks-claude-sonnet-4`` model + auto-
# picked claude-sdk harness with openai-agents + a Databricks
# OpenAI-compatible model. The bug reproduces regardless of
# harness — it lives in the AP-side approval event route — and
# openai-agents has the most reliable ``-p`` / REPL paths under
# the e2e fixtures' test-profile credentials.
_MODEL = "mock-model"
_HARNESS = "openai-agents"

# Cold boot + 4 LLM-driven tool calls + ASK round-trip + final
# response. The first turn is doing real LLM work + 4 tool
# dispatches; the elicitation parking only fires on the 4th
# call. 240 s is generous but still surfaces a wedged turn.
_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 120.0
_PRE_APPROVAL_TIMEOUT = 240.0
_POST_APPROVAL_TIMEOUT = 120.0
_EXIT_TIMEOUT = 15.0

# The prompt string. Asks for 4 distinct queries. The
# rate-limit policy allows 3 free; the 4th hits ASK. Wording
# nudges the model to issue four explicit tool calls rather
# than collapsing into one query.
_FOUR_SEARCH_PROMPT = (
    "Please run web searches for these four animals as separate "
    "search_web tool calls, one per query: octopus, cat, dog, "
    "elephant. Issue all four tool calls, do not combine them."
)


def test_run_omnigent_rate_limit_approval_round_trip(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Drive the rate-limit-search agent's ASK approval through a
    real PTY.

    Asserts:
      - The 4th tool call triggers an ``approval required`` banner.
      - The REPL accepts ``y`` and the approval event route returns
        successfully (no ``POST approval event failed``
        warning leaks into the buffer).
      - After approval, the 4th search actually runs (the result
        panel for ``elephant`` is rendered).

    Failure mode (pre-fix): banner appears, ``y`` typed, but the
    SDK logs ``POST approval event failed for elicitation_id
    ...: {error: internal_error}``. The agent then renders a
    ``[Denied by policy: ...]`` sentinel ~30s later when the
    parked workflow's ``ask_timeout`` fires.

    The mock LLM is configured to issue 4 ``search_web`` tool
    calls (octopus, cat, dog, elephant) in sequence so the
    rate-limit policy triggers on the 4th, exercising the full
    ASK → approve → run approval flow deterministically.

    :param omnigent_python: Path to the worktree's
        ``.venv/bin/python``.
    :param omnigent_repo_root: Repo root the subprocess uses as
        cwd so YAML ``callable:`` entries resolve on sys.path.
    :param mock_credentials_env: Mock-LLM env vars pointing at
        the mock server.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    from tests.e2e.omnigent.conftest import configure_mock_llm

    # Issue 4 search_web tool calls in sequence (3 allowed, 4th hits ASK).
    # After each tool result the mock server needs another response.
    # Sequence: call 1 → call 2 → call 3 → call 4 (ASK fires here,
    # approval is typed by the test, then call 4 runs) → final text.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "c1",
                        "name": "search_web",
                        "arguments": '{"query": "octopus"}',
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "c2",
                        "name": "search_web",
                        "arguments": '{"query": "cat"}',
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "c3",
                        "name": "search_web",
                        "arguments": '{"query": "dog"}',
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "c4",
                        "name": "search_web",
                        "arguments": '{"query": "elephant"}',
                    }
                ]
            },
            {"text": "I searched for octopus, cat, dog, and elephant."},
        ],
    )
    yaml_path = omnigent_repo_root / _YAML_REL

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    # ``logfile_read`` mirrors every byte pexpect reads from the
    # child into our buffer, so post-expect content (e.g. the
    # ``approved`` echo line that the REPL writes between
    # ``submit_prompt('y')`` and the next agent-side render)
    # is captured even though no expect waits for it. Without
    # this, ``child.before`` would only contain pre-match
    # content of the most recent expect and we'd miss the
    # verdict echo entirely.
    captured = io.StringIO()
    child.logfile_read = captured

    try:
        # Wait for the ``❯`` prompt marker rather than
        # ``state: sleeping``. The bottom-toolbar status line
        # depends on prompt-toolkit's CPR probe completing,
        # which doesn't always paint under pexpect — the ❯
        # marker is what the user actually sees before typing
        # so it's the right boot-ready signal. (Same workaround
        # documented in ``test_repl_inline_tool_streaming.py``.)
        child.expect(r"❯ ", timeout=_BOOT_TIMEOUT)

        submit_prompt(child, _FOUR_SEARCH_PROMPT)

        # Wait for the approval banner. The exact ``approval
        # required · tool_call`` substring is rendered by
        # :func:`omnigent.repl._repl._make_elicitation_prompt`'s
        # banner output. Anchor on a stable substring rather
        # than the full Unicode-prefixed line so a future
        # styling tweak doesn't false-positive break the test.
        child.expect("approval required", timeout=_PRE_APPROVAL_TIMEOUT)

        # Approve once. The REPL's ``on_input`` handler routes
        # any text typed while ``approval_state.pending`` is
        # True directly into the verdict future (not the agent
        # session). ``y`` parses as :class:`_ApprovalVerdict.APPROVE_ONCE`.
        submit_prompt(child, "y")

        # The verdict-echo line ``› approved`` is written
        # synchronously by ``on_input`` before resolving the
        # verdict future. Anchoring here proves the ``y`` was
        # treated as a verdict (not a normal message) AND that
        # the approval event route accepted it (the future is only
        # resolved after the SDK gets a 202 from the AP).
        child.expect("approved", timeout=_POST_APPROVAL_TIMEOUT)

        # The fix path: the route returns 202, the parked
        # workflow wakes immediately, and the 4th tool call
        # executes. The result panel for "elephant" is the
        # cleanest unique signal — it wasn't in any of the
        # first three calls and only appears if the 4th call
        # was actually dispatched.
        child.expect("elephant", timeout=_POST_APPROVAL_TIMEOUT)

        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    combined = strip_ansi(captured.getvalue())

    # The load-bearing assertion: the SDK never logged
    # the verdict-POST-failed warning. That warning is the
    # exact signature of the route's 500. If it appears here,
    # the route handler regressed to calling sync DBOS APIs
    # from the async path.
    assert "POST approval event failed" not in combined, (
        "REPL buffer contains 'POST approval event failed' — "
        "the approval event route returned 500 instead of 202. "
        "Most likely the route reverted to making a blocking call "
        "from the async handler; wrap it in asyncio.to_thread.\n"
        f"Combined output (last 4000 chars):\n{combined[-4000:]}"
    )

    # Belt-and-suspenders: the denial sentinel must not appear.
    # Pre-fix, the approved verdict timed out and got reclassified
    # as refused → ``[Denied by policy: ...]`` on the elephant
    # call. After fix, only the result panel appears.
    assert "Denied by policy" not in combined, (
        "REPL rendered a 'Denied by policy' sentinel even though the "
        "user typed 'y'. The verdict POST likely succeeded but the "
        "parked workflow timed out before reading the row. "
        f"Combined output (last 4000 chars):\n{combined[-4000:]}"
    )

    # Sanity: the 'approved' echo line that the REPL writes
    # right after the user types 'y' (see
    # ``_repl.py::on_input``'s ``› approved`` output) must be
    # in the buffer. If absent, the REPL's verdict-routing
    # logic regressed and the 'y' was treated as a normal
    # message instead of an approval.
    assert "approved" in combined, (
        "REPL did not echo 'approved' after the user typed 'y'. The "
        "verdict-handling branch of on_input may have regressed.\n"
        f"Combined output (last 4000 chars):\n{combined[-4000:]}"
    )
