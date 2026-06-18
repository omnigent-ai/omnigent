"""
REPL approval-flow e2e test.

Spawns ``omnigent chat examples/agents/ask-demo/`` as a subprocess
under a pseudo-TTY (pexpect), feeds real input, and asserts
the agent responds after the user approves a policy ASK.
This exercises the full Phase 10 path — prompt_toolkit's
real input loop, the SSE stream consuming ``ElicitationRequest``
events, the REPL's future-based approval wiring, and the
server PATCHing the verdict back through DBOS.

Unlike ``test_policies_e2e.py`` (polling API, background=True),
this test drives the REPL through the actual streaming code
path — the code path a human types into at the terminal.

Prerequisites:
    - ``pexpect`` installed (4.9+).
    - ``--llm-api-key`` pytest option set to a valid key for
      ``openai/gpt-4o``.
    - ``ap`` on ``PATH`` resolving to this worktree's entry
      point (set ``PYTHONPATH`` so the editable install from
      a sibling worktree doesn't shadow it).

Usage::

    PYTHONPATH=/home/ubuntu/omnigent-policies:\\
    /home/ubuntu/omnigent-policies/sdks/python-client:\\
    /home/ubuntu/omnigent-policies/sdks/frontend \\
    python -m pytest tests/e2e/test_repl_approval_e2e.py \\
      --llm-api-key $(cat /tmp/mykey) -v
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

pexpect = pytest.importorskip("pexpect")

_ASK_DEMO_DIR = Path(__file__).resolve().parents[1] / "resources" / "agents" / "ask-demo"
_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "_fixtures" / "agents"
_TOOL_GATE_DIR = _FIXTURES_DIR / "e2e-tool-gate"
_SUBAGENT_GATE_DIR = _FIXTURES_DIR / "e2e-subagent-gate"
_LABEL_ASK_GATE_DIR = _FIXTURES_DIR / "e2e-label-ask-gate"
_OUTPUT_GATE_DIR = _FIXTURES_DIR / "e2e-output-gate"
_TOOL_RESULT_GATE_DIR = _FIXTURES_DIR / "e2e-tool-result-gate"
_SUBAGENT_TOOL_GATE_DIR = _FIXTURES_DIR / "e2e-subagent-tool-gate"

# Regex to strip ANSI escape codes from pexpect output before
# asserting. prompt_toolkit emits heavy styling — searching for
# substrings ("approval required", "Hi") against the raw bytes
# finds them most of the time but is flaky on split sequences.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """
    Remove ANSI escape codes from a pexpect buffer slice.

    :param text: Captured output with escape sequences.
    :returns: Plain text suitable for substring assertions.
    """
    return _ANSI_RE.sub("", text)


@pytest.fixture(scope="module")
def repl_env(llm_api_key: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """
    Build the env dict for ``omnigent chat`` — OPENAI_API_KEY plus
    whatever PYTHONPATH the outer shell already provides (so
    ``omnigent`` + ``omnigent_client`` resolve to this
    worktree, not the sibling editable install).

    Redirects ``HOME`` to a temp dir seeded with
    ``.omnigent/config.yaml`` so the spawned interactive REPL starts
    cleanly under pexpect:

    - ``tui.theme`` is persisted, so the REPL's first-launch theme
      picker (``_repl._load_startup_theme`` → ``startup_theme_picker``,
      which reads ``$HOME/.omnigent/config.yaml`` — NOT
      ``OMNIGENT_CONFIG_HOME``) is skipped. Under pexpect's pty stdin
      is a tty, so without a persisted theme the arrow-key picker blocks
      and the welcome banner never appears (the CI failure, where
      ``$HOME`` is fresh).
    - ``auto_open_conversation: false`` stops the interactive REPL from
      opening a browser tab per run (``--no-open`` is not a valid
      ``run`` flag; config is the supported path). ``OMNIGENT_CONFIG_HOME``
      points at the same dir so the CLI reads it too.

    Because ``HOME`` is redirected, ``DATABRICKS_CONFIG_FILE`` is pinned
    to the real ``~/.databrickscfg`` so ``--profile`` lookups still
    resolve, and ``OMNIGENT_SKIP_ONBOARD`` guards against any other
    first-run prompt (these tests exercise REPL approval, not onboarding).

    :param llm_api_key: The API key for the LLM.
    :param tmp_path_factory: Pytest temp-path factory for the fake HOME.
    :returns: Env mapping for ``pexpect.spawn``.
    """
    real_databrickscfg = Path.home() / ".databrickscfg"
    fake_home = tmp_path_factory.mktemp("repl_home")
    config_home = fake_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n"
    )
    env: dict[str, str] = {
        **os.environ,
        "OPENAI_API_KEY": llm_api_key,
        "HOME": str(fake_home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "DATABRICKS_CONFIG_FILE": str(real_databrickscfg),
        "OMNIGENT_SKIP_ONBOARD": "1",
        # Force ANSI on — pexpect captures everything, stripping
        # happens per-assertion via _strip_ansi.
        "TERM": "xterm-256color",
        # Disable prompt_toolkit's alt-screen / mouse reporting
        # so the buffer doesn't fill with cursor-position-query
        # sequences that throw off expect matches.
        "PROMPT_TOOLKIT_NO_CPR": "1",
    }
    return env


def _require_omnigent_cli() -> str:
    """
    Resolve the CLI path. Prefers the framework's own
    ``omnigent`` binary (via the running pytest interpreter's
    venv) over a sibling ``ap`` binary on PATH — the legacy
    ``omnigent`` ``ap`` CLI doesn't understand
    Omnigent-format fixtures.

    :returns: Absolute path to an executable.
    """
    venv_omnigent = Path(sys.executable).parent / "omnigent"
    if venv_omnigent.exists():
        return str(venv_omnigent)
    path = shutil.which("omnigent") or shutil.which("ap")
    if path is None:
        pytest.skip("Neither omnigent nor omnigent CLI on PATH")
    return path


@pytest.fixture(scope="module")
def ap_cli() -> str:
    """Session-scoped resolved ``ap`` binary."""
    return _require_omnigent_cli()


def _wait_for_prompt_ready(
    child: Any,
    timeout: float = 30.0,
    welcome_pattern: str = "ask.demo",
) -> None:
    """
    Wait until the REPL is ready for input.

    ``omnigent chat <path>`` starts a local server, waits for
    health, then launches the REPL. The welcome block
    (TimedFormatter renders the agent name with dashes →
    spaces) renders BEFORE prompt_toolkit's input loop is
    live — matching only the banner and sending immediately
    races the submit ahead of the input loop, so the
    keystroke is dropped and the turn never starts.

    The reliable input-readiness signal is the bottom status
    toolbar, which renders ``· ready`` (with ``state:
    sleeping``) once prompt_toolkit's application is running
    and idle. Waiting for that after the banner makes the
    subsequent ``child.send(...)`` land in the live input
    loop. Using a generous timeout — agent upload + DBOS boot
    add latency on cold starts.

    :param child: Active pexpect child.
    :param timeout: Max seconds to wait.
    :param welcome_pattern: Regex pattern to match in the
        welcome block. Defaults to ``"ask.demo"`` (for the
        ``ask-demo`` fixture); pass a different pattern for
        other fixtures.
    """
    child.expect(welcome_pattern, timeout=timeout)
    # The banner is not input-readiness. Wait for the status
    # toolbar's ``· ready`` (idle ``state: sleeping``) marker
    # so the next send() lands in prompt_toolkit's live loop
    # instead of racing ahead of it.
    child.expect(r"·\s*ready", timeout=timeout)


def _wait_for_turn_complete(child: Any, timeout: float = 45.0) -> None:
    """
    Block until the current turn has finished streaming.

    The REPL's bottom toolbar is the turn-state oracle: it reads
    ``state: running`` (animated spinner) while a handler task is
    in flight and flips back to ``state: sleeping`` (``· ready``)
    once the turn fully lands. The older tests waited on a
    ``\\d+\\.\\d+s`` decimal "elapsed" footer, but the current REPL
    never renders one — the only elapsed readout is the integer
    ``streaming… Ns`` segment that disappears on completion. Waiting
    for that stale pattern times out even though the turn finished,
    which is the second half of the REPL-pexpect quarantine family.

    A single ``· ready`` expect is enough: every send/approve site
    leaves the toolbar in ``state: running`` (the turn is dispatched,
    or an approval is pending), so the next ``· ready`` is the
    post-turn idle settle — never a stale pre-turn toolbar. Keeping it
    to one expect also preserves ``child.before`` as the full
    span of the turn's rendered output, which several callers scan.

    :param child: Active pexpect child.
    :param timeout: Max seconds to wait for the turn to settle.
    """
    child.expect(r"·\s*ready", timeout=timeout)


def _read_pending(child: Any, seconds: float = 0.2) -> str:
    """
    Non-blocking read of everything buffered so far.

    :param child: pexpect child.
    :param seconds: Small timeout so the call returns promptly
        after the buffer is drained.
    :returns: Whatever pexpect had queued, stripped of ANSI.
    """
    with contextlib.suppress(pexpect.EOF):
        child.expect(pexpect.TIMEOUT, timeout=seconds)
    captured = child.before or ""
    if isinstance(captured, bytes):
        captured = captured.decode("utf-8", errors="replace")
    return _strip_ansi(captured)


def test_repl_single_approval_allows_llm_response(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Drive the full approval → LLM → response loop through the
    REPL.

    Scenario: the ``ask-demo`` agent declares
    ``always_ask_on_input`` (a policy at INPUT that
    always ASKs). We send "Hello", expect the approval
    prompt, type "y", and expect the LLM's real reply.

    Why this is the right test layer: unit tests can stub the
    approval hook, but only a real pexpect run proves the
    end-to-end stack — prompt_toolkit's raw keystroke
    handling, the SDK's ``ElicitationRequest`` event routing,
    the server's ``response.elicitation_request`` emission, the
    session ``approval`` event reply path, and DBOS wake
    semantics — all cohere in production.

    Load-bearing assertion: EXACTLY ONE approval prompt. The
    "three approvals for one message" bug (prior bug:
    ``_enforce_input_policies`` walked history from index 0
    each invocation) would fail this test by rendering
    multiple ``⚠ approval required`` banners. Counting on
    the ANSI-stripped buffer is the regression guard.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_ASK_DEMO_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),  # rows, cols
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(child, timeout=60)

        # Send the user message and wait for the approval
        # banner. 'approval required' is the human-readable
        # header emitted by the REPL's _make_approval_prompt.
        child.send("Hello" + "\r")
        child.expect("approval required", timeout=30)
        # The preview line should echo what we just typed —
        # confirms the server-side INPUT-phase eval and the
        # client-side SSE parsing both agreed on the payload.
        child.expect("Hello", timeout=5)

        # Approve. Any input while an approval is pending is
        # routed to the verdict future — no special slash
        # command, just "y".
        child.send("y" + "\r")
        # Echo line confirms the REPL resolved the verdict
        # (sanity on the main-loop routing).
        child.expect("approved", timeout=5)

        # Now expect the LLM's actual reply. gpt-4o against
        # the ask-demo AGENTS.md should produce a short
        # greeting ("Hi", "Hello", etc.). We assert on a
        # minimal substring that any reasonable reply
        # contains — the test isn't asserting what the model
        # says, only that SOMETHING of non-trivial length
        # arrived after approval.
        child.expect(pexpect.TIMEOUT, timeout=8)
        buffered = _read_pending(child, seconds=2.0)
        # Drain a little more in case the response is still
        # streaming in chunks.
        buffered += _read_pending(child, seconds=3.0)

        # Exactly one approval banner — regression guard for
        # the "three approvals for one message" bug.
        approval_count = buffered.count("approval required")
        # The `.expect("approval required")` above already
        # consumed the first banner from pexpect's buffer,
        # so anything here would be an extra. Zero is the
        # correct assertion.
        assert approval_count == 0, (
            "Saw "
            f"{approval_count} extra approval banners after the first — "
            "`_enforce_input_policies` re-firing on same message?\n"
            f"Buffer snippet:\n{buffered[:800]}"
        )
        # The agent replied with some text. We don't know the
        # exact wording, but the AGENTS.md asks for a brief
        # greeting, so any reasonable reply contains some
        # letters after the approval.
        assert re.search(r"[A-Za-z]{3,}", buffered), (
            f"No LLM response text appeared after approval.\nBuffer snippet:\n{buffered[:800]}"
        )
    finally:
        # Best-effort clean shutdown — /quit is the REPL's
        # documented exit command, but if it's stuck we fall
        # back to SIGTERM.
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_refusal_shows_deny_sentinel(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Same flow, user refuses → server substitutes the DENY
    sentinel → that text lands as the assistant reply.

    This proves the fail-closed path end-to-end: hook returns
    False → SDK POSTs a session ``approval`` event → server's
    ``_await_elicitation`` parses verdict, hits the DENY
    branch, ``_enforce_input_policies`` returns the
    ``[Denied by policy: ...]`` sentinel, and
    ``_persist_input_deny_sentinel`` surfaces it as the
    assistant message the REPL renders.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_ASK_DEMO_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(child, timeout=60)

        child.send("Hello" + "\r")
        child.expect("approval required", timeout=30)
        child.expect("Hello", timeout=5)

        # Refuse. Typing anything non-affirmative refuses —
        # "n" is the natural keyboard muscle memory.
        child.send("n" + "\r")
        child.expect("refused", timeout=5)

        # The server emits the DENY sentinel as the assistant
        # reply. Exact reason string is shaped by the
        # Policy spec in ask-demo/config.yaml
        # ("Confirm this message before I process it.").
        child.expect(r"Denied by policy", timeout=10)
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_two_turns_fires_one_approval_per_turn(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Regression guard for the multi-turn duplicate-ASK bug.

    Scenario: two consecutive turns in the same conversation.
    Each turn must fire EXACTLY ONE approval. The bug this
    pins: `_enforce_input_policies` previously walked history
    from index 0 on every new workflow, re-ASKing historical
    user messages from prior turns.

    The fix: skip past the last assistant message on fresh
    invocation. The fact that we only see one approval on
    turn 2 proves the prior user message from turn 1 is NOT
    being re-enforced.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_ASK_DEMO_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(child, timeout=60)

        # Turn 1: approve, wait for reply.
        child.send("Hello" + "\r")
        child.expect("approval required", timeout=30)
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        # Wait for the turn to fully land — the stream-done
        # elapsed-time label is the cleanest signal.
        _wait_for_turn_complete(child, timeout=30)

        # Drain anything queued so the next expect starts
        # from a clean slate. Generous wait because the REPL
        # emits a flurry of cursor-position codes after the
        # response completes — we want them all absorbed
        # before sending the next input.
        _read_pending(child, seconds=1.5)

        # Turn 2: a brand-new message in the same
        # conversation. If the old bug were present, the
        # REPL would render TWO approval banners here (one
        # for the historical "Hello", one for "kk"). The
        # fix means exactly one banner appears — for "kk".
        child.send("kk" + "\r")
        # Capture the buffer from the send through the
        # approval banner so we can inspect the preview line
        # — pexpect's .expect on "preview:\\s*kk" has been
        # flaky against heavily-styled output. Match on the
        # banner, then scan the drained buffer afterwards.
        child.expect("approval required", timeout=30)
        # Pull the remaining banner text (policy / reason /
        # preview / prompt line) into a buffer we can assert
        # against with substring checks after ANSI stripping.
        banner_tail = _read_pending(child, seconds=1.5)
        assert "kk" in banner_tail, (
            "Turn 2 banner's preview did not contain 'kk' — the fix for "
            "`_enforce_input_policies` re-firing on historical messages "
            "may have regressed.\n"
            f"Tail captured (ANSI-stripped):\n{banner_tail[:800]}"
        )
        # And the banner MUST NOT show 'Hello' as its preview —
        # that would be the historical-message regression.
        assert "preview: Hello" not in banner_tail, (
            "Turn 2's approval is previewing the prior turn's 'Hello' — "
            "`_enforce_input_policies` re-firing on historical messages.\n"
            f"Tail:\n{banner_tail[:800]}"
        )

        # Approve and confirm one-and-done.
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        _wait_for_turn_complete(child, timeout=30)

        # Final sweep: no extra approval banners after the
        # two we expected.
        buffered = _read_pending(child, seconds=1.0)
        extras = buffered.count("approval required")
        assert extras == 0, f"Unexpected extra approval banner after turn 2:\n{buffered[:800]}"
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_approve_always_caches_for_later_turns(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    End-to-end coverage for the "approve always" cache.

    Turn 1: user types "a" at the approval prompt. The
    ``_ApprovalState`` caches ``(always_ask_on_input, input)``
    for this REPL session.

    Turn 2: the same policy fires at the same phase. The hook
    short-circuits on the cache — prints a muted
    ``auto-approved`` audit line and returns True WITHOUT
    rendering the ``⚠ approval required`` banner. The LLM
    proceeds as if the user pre-approved.

    Load-bearing assertions:

    1. Turn 2 must show ``auto-approved`` in the transcript —
       silent auto-approve would be security-hostile (users
       forget they flipped "always" on).
    2. Turn 2 must NOT show ``⚠ approval required`` — that's
       the whole point of the cache; a user who typed "a"
       expects no more prompting for this policy in this
       session.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_ASK_DEMO_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(child, timeout=60)

        # Turn 1: approve always.
        child.send("Hello" + "\r")
        child.expect("approval required", timeout=30)
        child.send("a" + "\r")
        # Echo line confirms the REPL parsed "a" as
        # APPROVE_ALWAYS, not as a generic non-"y" refusal.
        child.expect("approved always", timeout=5)
        _wait_for_turn_complete(child, timeout=30)

        # Drain between turns so the next buffer is clean.
        _read_pending(child, seconds=1.5)

        # Turn 2: the auto-approved audit line must appear
        # AND the banner must NOT. After .expect() lands on
        # the elapsed-time marker, ``child.before`` holds the
        # full span from the last expect up to (but not
        # including) the match. That's the whole turn 2
        # output — banner (if any) + auto-approved line (if
        # any) + LLM response + elapsed-time prefix.
        child.send("follow up please" + "\r")
        _wait_for_turn_complete(child, timeout=45)
        turn_two_raw = child.before or ""
        if isinstance(turn_two_raw, bytes):
            turn_two_raw = turn_two_raw.decode("utf-8", errors="replace")
        turn_two = _strip_ansi(turn_two_raw)

        assert "auto-approved" in turn_two, (
            "Turn 2 did not render the auto-approve audit line.\n"
            f"Captured (ANSI-stripped, {len(turn_two)} chars):\n{turn_two[:1500]}"
        )
        assert "approval required" not in turn_two, (
            "Turn 2 rendered the approval banner even though the user "
            "said 'always' on turn 1 — cache lookup is broken.\n"
            f"Captured:\n{turn_two[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


# ── TOOL_CALL-phase approval coverage ─────────────────────
#
# Phase 6 wired the TOOL_CALL enforcement site in
# ``_execute_tools``. These tests prove the full round-trip:
# user message → LLM emits tool_call → policy ASKs → server
# parks → SSE surfaces ``response.elicitation_request`` →
# REPL renders → user answers → SDK POSTs a session approval
# event → server wakes the parked workflow → tool dispatches (on approve)
# or sentinel replaces output (on refuse).


def test_repl_tool_call_approval_allows_tool_to_run(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    TOOL_CALL ASK → approve → tool runs → LLM responds.

    The ``e2e-tool-gate`` fixture's AGENTS.md instructs the
    LLM to call the ``echo`` tool for every user message.
    The policy ``ask_before_echo`` ASKs on every
    ``tool_call:echo``. After the user approves, the tool
    runs and its output (prefixed ``echo:``) flows back to
    the LLM, which includes it in the final reply.

    The banner's ``phase`` field must be ``tool_call`` — not
    ``input`` — which is the critical distinction from the
    INPUT-phase tests above. Proves the TOOL_CALL site is
    wired and end-to-end correct.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_TOOL_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(child, timeout=60, welcome_pattern="e2e.tool.gate")
        child.send("testing123" + "\r")
        child.expect("approval required", timeout=45)
        banner_tail = _read_pending(child, seconds=1.0)
        # Must be the TOOL_CALL phase (not INPUT) — this is
        # the whole point of the test.
        assert "tool_call" in banner_tail, (
            "Banner phase field was not 'tool_call' — the ASK may have "
            "fired at a different phase than expected.\n"
            f"Banner tail:\n{banner_tail[:800]}"
        )
        # Policy name and echo tool should be on the banner.
        assert "ask_before_echo" in banner_tail, (
            f"Policy name missing from banner.\nBanner:\n{banner_tail[:800]}"
        )
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        # Wait for turn completion (elapsed-time marker).
        _wait_for_turn_complete(child, timeout=45)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # The echo tool runs; its output prefix 'echo:' should
        # reach the LLM's reply (the AGENTS.md tells it to
        # include the tool's output).
        assert "echo:" in full_turn or "testing123" in full_turn, (
            f"Tool output did not make it into the LLM's reply.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_tool_call_refusal_blocks_tool(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    TOOL_CALL ASK → refuse → tool NEVER runs → sentinel
    replaces output → LLM sees sentinel and typically relays
    that denial to the user.

    Load-bearing: the raw tool output MUST NOT reach the
    conversation — ``_enforce_tool_result_policy`` substitutes
    ``[Denied by policy: ...]``. This test is the end-to-end
    proof that the pre-persistence ordering holds under real
    streaming + DBOS parking.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_TOOL_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(child, timeout=60, welcome_pattern="e2e.tool.gate")
        child.send("testing456" + "\r")
        child.expect("approval required", timeout=45)
        child.send("n" + "\r")
        child.expect("refused", timeout=5)
        # Wait for the turn to complete. The LLM sees
        # the blocked sentinel as the tool output, then
        # either reports the denial or stops. Elapsed-time
        # marker signals the turn ended.
        _wait_for_turn_complete(child, timeout=60)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # The sentinel must appear in the tool output path —
        # this is the regression guard for the pre-persist
        # ordering invariant.
        assert "Denied by policy" in full_turn, (
            "Tool result sentinel did not appear in the turn — "
            "pre-persistence enforcement may have regressed.\n"
            f"Captured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


# ── Sub-agent approval tunneling ──────────────────────────
#
# When a sub-agent hits an ASK, the parked workflow is the
# SUB-AGENT's, but the ``response.elicitation_request`` must
# surface on the ROOT task's SSE stream so the REPL (which
# is attached to the root) sees it. This is the same
# tunneling path client-side tool calls use from within
# sub-agents — POLICIES.md §7 / workflow.py's
# ``_handle_policy_ask`` ``publish_target`` computation.


def test_repl_subagent_ask_tunnels_approval_to_root(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Sub-agent INPUT ASK → approval on ROOT SSE stream →
    REPL approves → sub-agent runs → parent integrates the
    sub-agent's reply and finishes the turn.

    Load-bearing:

    - The banner must appear on the root REPL — proves
      ``root_task_id``-based tunneling for the synthetic
      function_call works exactly like for client-side
      tool calls.
    - The banner's phase must be ``input`` — the sub-agent's
      gate, not the parent's. Both the policy_name and the
      phase field come from the SUB-AGENT's spec, so
      matching ``worker_input_gate`` + ``input`` on the
      banner proves the right engine fired.
    - After approving, the parent's reply must exist —
      proves the wake path unblocks the sub-agent, its LLM
      runs, the result flows to the parent, and the parent
      composes the final response.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_SUBAGENT_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=90,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=90,
            welcome_pattern="e2e.subagent.gate",
        )
        child.send("say hello" + "\r")
        # The approval banner may take a bit longer because
        # spawn + sub-agent boot fires first.
        child.expect("approval required", timeout=60)
        banner_tail = _read_pending(child, seconds=1.5)
        # Phase must be INPUT (the sub-agent's INPUT site),
        # policy_name must be the sub-agent's policy. These
        # two together prove the routing path: the ASK came
        # from the WORKER's engine, surfaced on the ROOT
        # stream.
        assert "input" in banner_tail, (
            "Sub-agent ASK banner did not show phase=input — routing may "
            "have attached the wrong phase or the ASK never tunneled "
            "to the root SSE stream.\n"
            f"Banner:\n{banner_tail[:800]}"
        )
        assert "worker_input_gate" in banner_tail, (
            "Sub-agent's policy name missing from root-surfaced banner — "
            "tunneling may have confused root/sub-agent identity.\n"
            f"Banner:\n{banner_tail[:800]}"
        )
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        # Let the full turn complete — sub-agent runs, returns,
        # parent summarizes, turn ends.
        _wait_for_turn_complete(child, timeout=90)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # Some LLM text arrived after the approval — the
        # parent's final reply. Exact wording depends on the
        # model, but we can assert at least a few words
        # appeared (words with 3+ letters).
        assert re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}", full_turn), (
            "Parent never produced a final reply after sub-agent "
            f"approval.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


# ── Label-driven ASK composition ──────────────────────────
#
# Tests the two-turn chain:
# - Turn 1 with a trigger token: FunctionPolicy ALLOWs and
#   writes a taint label.
# - Turn 2: Policy with ``condition: {tainted: "1"}``
#   fires ASK because the label persisted across the
#   workflow boundary.
#
# Complements ``test_label_gate_*`` in test_policies_e2e.py
# which cover the DENY variant via the polling API.


def test_repl_label_driven_ask_approves(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Two-turn label-ASK composition, approve path.

    Turn 1: user message contains ``BANANA_TRIGGER``. The
    FunctionPolicy writes ``tainted: "1"``; the gated policy's
    condition checks the pre-evaluation snapshot so does NOT
    fire yet. LLM responds normally.

    Turn 2: any message. The persisted label makes the
    Policy condition match → ASK. User approves → LLM
    runs normally for the second turn.

    Load-bearing: proves (a) FunctionPolicy label writes
    persist to the store and survive the sub-agent /
    workflow restart, (b) condition gates read
    the live cache on turn 2, (c) ASK composition with a
    write in the chain doesn't leak the write on refuse
    (that's a separate refuse test below).
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_LABEL_ASK_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=60,
            welcome_pattern="e2e.label.ask.gate",
        )
        # Turn 1: trigger taint — no ASK fires this turn
        # (condition checks the pre-evaluation snapshot).
        child.send("hello BANANA_TRIGGER" + "\r")
        # The LLM still replies normally. Wait for turn end.
        _wait_for_turn_complete(child, timeout=45)
        turn_one = child.before or ""
        if isinstance(turn_one, bytes):
            turn_one = turn_one.decode("utf-8", errors="replace")
        turn_one = _strip_ansi(turn_one)
        # Turn 1 MUST NOT show an approval banner — the
        # taint label didn't exist when the condition was
        # checked.
        assert "approval required" not in turn_one, (
            "Turn 1 fired an ASK before the taint label was set — "
            "condition gate is reading the post-write snapshot.\n"
            f"Turn 1:\n{turn_one[:1500]}"
        )

        _read_pending(child, seconds=1.0)

        # Turn 2: label persists from the store → condition
        # matches → ASK fires.
        child.send("please continue" + "\r")
        child.expect("approval required", timeout=45)
        banner_tail = _read_pending(child, seconds=1.0)
        assert "ask_when_tainted" in banner_tail, (
            "Turn 2's banner didn't come from the label-gated policy.\n"
            f"Banner:\n{banner_tail[:800]}"
        )
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        # Turn 2 completes — LLM replies normally.
        _wait_for_turn_complete(child, timeout=45)
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_label_driven_ask_refuse_shows_sentinel(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Same composition, refuse path.

    Turn 2's ASK refused → server substitutes the DENY
    sentinel → REPL shows ``Denied by policy``. Proves the
    label-gated ASK's refuse branch goes through the same
    pre-persist sentinel path as INPUT DENY.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_LABEL_ASK_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=60,
            welcome_pattern="e2e.label.ask.gate",
        )
        # Turn 1: taint.
        child.send("hi BANANA_TRIGGER" + "\r")
        _wait_for_turn_complete(child, timeout=45)
        _read_pending(child, seconds=1.0)

        # Turn 2: ASK fires, user refuses.
        child.send("anything" + "\r")
        child.expect("approval required", timeout=45)
        child.send("n" + "\r")
        child.expect("refused", timeout=5)
        _wait_for_turn_complete(child, timeout=45)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        assert "Denied by policy" in full_turn, (
            "Refused label-gated ASK did not produce a DENY sentinel.\n"
            f"Captured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


# ── OUTPUT-phase approval coverage ────────────────────────
#
# POLICIES.md §11.4: the raw assistant text must never reach
# ``conversation_items`` when OUTPUT policy DENYs —
# compaction could resurface it otherwise. These tests prove
# the pre-persistence ordering holds end-to-end when the user
# actually refuses the assistant reply.


def test_repl_output_ask_approve_surfaces_llm_reply(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    OUTPUT ASK → approve → LLM reply appears verbatim.

    Proves the OUTPUT enforcement site in
    ``_handle_final_response`` doesn't mangle the text on
    approve — the original ``text`` passes through the
    helper unchanged and lands in the assistant message.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_OUTPUT_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=60,
            welcome_pattern="e2e.output.gate",
        )
        child.send("say hi" + "\r")
        # OUTPUT ASK fires AFTER the LLM generates. The
        # banner's phase must be ``response``.
        child.expect("approval required", timeout=45)
        banner_tail = _read_pending(child, seconds=1.0)
        assert "response" in banner_tail, (
            f"RESPONSE-phase banner missing 'response' phase marker.\nBanner:\n{banner_tail[:800]}"
        )
        assert "ask_on_output" in banner_tail, (
            f"Policy name missing.\nBanner:\n{banner_tail[:800]}"
        )
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        _wait_for_turn_complete(child, timeout=45)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # The LLM reply arrives AFTER approve — at least a
        # real word (3+ letters) shows up somewhere. Short
        # greetings like "Hi there!" are valid replies.
        assert re.search(r"[A-Za-z]{3,}", full_turn), (
            f"No LLM reply text appeared after OUTPUT approve.\nCaptured:\n{full_turn[:1500]}"
        )
        # Critical: OUTPUT approve must NOT surface a DENY
        # sentinel — regression guard for the helper
        # substituting text on the wrong branch.
        assert "Denied by policy" not in full_turn, (
            f"OUTPUT approve path leaked a DENY sentinel.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_output_ask_refuse_replaces_reply_with_sentinel(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    OUTPUT ASK → refuse → assistant message = sentinel.

    The user sees ``[Denied by policy: ...]`` instead of the
    LLM's real reply. The REAL text must never land in
    ``conversation_items`` — pre-persistence ordering
    invariant from POLICIES.md §11.4. A follow-up turn only
    sees the sentinel in history.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_OUTPUT_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=60,
            welcome_pattern="e2e.output.gate",
        )
        child.send("say hi" + "\r")
        child.expect("approval required", timeout=45)
        child.send("n" + "\r")
        child.expect("refused", timeout=5)
        _wait_for_turn_complete(child, timeout=45)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        assert "Denied by policy" in full_turn, (
            f"OUTPUT refuse did not substitute the DENY sentinel.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


# ── TOOL_RESULT-phase approval coverage ───────────────────
#
# Distinct from TOOL_CALL: the policy fires AFTER the tool
# dispatches and returns, BEFORE the result reaches
# function_call_output. Tool output exfiltration is the
# canonical motivating case — "run the tool but I want to
# review what it returned before the LLM sees it".


def test_repl_tool_result_ask_approve_surfaces_tool_output(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    TOOL_RESULT ASK → approve → tool output reaches the LLM.

    Unlike the TOOL_CALL fixture, dispatch happens freely
    here; the ASK fires on the RESULT. On approve the
    original tool output (``echo: <input>``) flows back to
    the LLM which includes it in the final reply.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_TOOL_RESULT_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=60,
            welcome_pattern="e2e.tool.result.gate",
        )
        child.send("pineapple" + "\r")
        child.expect("approval required", timeout=45)
        banner_tail = _read_pending(child, seconds=1.0)
        # Must be TOOL_RESULT (not TOOL_CALL, not INPUT).
        assert "tool_result" in banner_tail, (
            "Banner phase was not tool_result — either the ASK fired at "
            "the wrong phase or the banner format regressed.\n"
            f"Banner:\n{banner_tail[:800]}"
        )
        # Preview should contain the echo tool's output
        # (``echo: pineapple``) — the TOOL_RESULT evaluator
        # passes the result dict as ctx.content.
        assert "echo" in banner_tail or "pineapple" in banner_tail, (
            f"Preview missing tool output.\nBanner:\n{banner_tail[:800]}"
        )
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        _wait_for_turn_complete(child, timeout=45)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # Tool output must flow to the LLM and appear in reply.
        assert "pineapple" in full_turn.lower() or "echo" in full_turn, (
            "Tool output did not reach the LLM's reply after TOOL_RESULT "
            f"approve.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_tool_result_ask_refuse_replaces_output(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    TOOL_RESULT ASK → refuse → tool output replaced by DENY
    sentinel before reaching function_call_output.

    The tool DID run (TOOL_RESULT fires after dispatch), but
    the LLM must see the sentinel in function_call_output,
    NOT the real output. Regression guard for the pre-
    persistence substitution in ``_execute_tools``.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_TOOL_RESULT_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=60,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=60,
            welcome_pattern="e2e.tool.result.gate",
        )
        child.send("mangosteen" + "\r")
        child.expect("approval required", timeout=45)
        child.send("n" + "\r")
        child.expect("refused", timeout=5)
        _wait_for_turn_complete(child, timeout=60)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        assert "Denied by policy" in full_turn, (
            "TOOL_RESULT refuse did not produce a DENY sentinel on the "
            f"tool output.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


# ── Sub-agent TOOL_CALL approval tunneling ────────────────
#
# The sub-agent fires an ASK from the TOOL_CALL phase (not
# INPUT). Still must surface on the ROOT SSE stream — the
# tunneling path is identical for every phase in the
# sub-agent's engine.


def test_repl_subagent_tool_call_ask_tunnels_to_root(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Sub-agent TOOL_CALL ASK → banner on root REPL → approve
    → sub-agent's tool runs → sub-agent replies → parent
    composes final turn.

    Load-bearing:

    - Banner phase must be ``tool_call`` (not ``input``) —
      proves the sub-agent's tool-phase engine fired.
    - Banner policy must be the sub-agent's
      ``worker_tool_gate`` (not the parent's non-existent
      gate).
    - Root REPL sees the banner through the same SSE stream
      it was already consuming.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_SUBAGENT_TOOL_GATE_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=90,
    )
    try:
        _wait_for_prompt_ready(
            child,
            timeout=90,
            welcome_pattern="e2e.subagent.tool.gate",
        )
        child.send("return the word durian" + "\r")
        child.expect("approval required", timeout=90)
        banner_tail = _read_pending(child, seconds=1.5)
        assert "tool_call" in banner_tail, (
            "Sub-agent TOOL_CALL ASK did not show phase=tool_call — "
            "routing may have surfaced the wrong phase.\n"
            f"Banner:\n{banner_tail[:800]}"
        )
        assert "worker_tool_gate" in banner_tail, (
            f"Sub-agent's tool-gate policy name missing from banner.\nBanner:\n{banner_tail[:800]}"
        )
        child.send("y" + "\r")
        child.expect("approved", timeout=5)
        _wait_for_turn_complete(child, timeout=120)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # Parent's final reply should contain something from
        # the sub-agent's reply, which used the tool output.
        assert re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}", full_turn), (
            "Parent never produced a final reply after sub-agent "
            f"TOOL_CALL approval.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)
