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

All 14 tests run against the mock LLM server: ``OPENAI_BASE_URL``
is injected into the REPL subprocess's environment so the inner
OpenAI harness routes to the mock server. Each test pre-configures
the mock's keyed response queue before spawning the subprocess.

Prerequisites:
    - ``pexpect`` installed (4.9+).
    - ``ap`` on ``PATH`` resolving to this worktree's entry
      point (set ``PYTHONPATH`` so the editable install from
      a sibling worktree doesn't shadow it).

Usage::

    python -m pytest tests/e2e/test_repl_approval_e2e.py -v --timeout=180
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    reset_mock_llm,
)

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


def _wait_for_function_call_outputs(
    mock_llm_server_url: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 0.5,
) -> str:
    """
    Poll the mock server until the tool round-trip's
    ``function_call_output`` is recorded, then return the joined outputs.

    Waits on the *exact* post-condition the callers assert on (a
    recorded ``function_call_output``) rather than sampling
    :func:`get_mock_requests` once after a proxy signal (the follow-up
    text rendering). The REPL can render the follow-up reply a beat
    before the mock server finishes persisting the request that carried
    the output, so a single sample races and returns ``''`` (~3% flake
    observed on CI shard 2). Polling the real signal removes the race;
    ``timeout`` is only a safety cap, not the thing we time against.

    :param mock_llm_server_url: Mock server URL.
    :param timeout: Max seconds to wait for the output to be recorded.
    :param poll_interval: Seconds between polls.
    :returns: Space-joined ``function_call_output`` values (``''`` if
        none were recorded within ``timeout``).
    """
    deadline = time.monotonic() + timeout
    while True:
        reqs = get_mock_requests(mock_llm_server_url)
        outputs = [
            item.get("output", "")
            for req in reqs
            for item in (req.get("input") or [])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        joined = " ".join(str(o) for o in outputs)
        if outputs or time.monotonic() >= deadline:
            return joined
        time.sleep(poll_interval)


@pytest.fixture(scope="module")
def repl_env(
    llm_api_key: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
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

    ``OPENAI_BASE_URL`` is pointed at the session-scoped mock LLM
    server so the REPL subprocess's inner OpenAI harness routes all
    completions through the mock instead of hitting ``api.openai.com``.

    :param llm_api_key: The API key for the LLM (``"mock-key"`` in
        mock mode).
    :param mock_llm_server_url: Base URL of the mock LLM server,
        e.g. ``"http://127.0.0.1:12345"``.
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
        # Point the inner OpenAI harness at the mock LLM server.
        # The SDK appends /responses to the base URL, so include /v1.
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
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


def _configure_mock_text(
    mock_llm_server_url: str, texts: list[str], *, match: str | None = None
) -> None:
    """
    Pre-load the mock LLM server with simple text responses.

    Resets all queues first, then configures a ``"default"`` queue
    with one ``QueuedResponse`` per string in *texts*. All agent
    fixtures use ``model: gpt-4o``, which falls through to the
    ``"default"`` queue on the mock server.

    :param mock_llm_server_url: Mock server base URL.
    :param texts: Ordered list of response texts the mock should
        return, one per LLM call.
    :param match: Optional content-routing token (the unique user
        message this test sends). When set, these responses are served
        only to requests whose user input contains the token, isolating
        this test's queue from a stray/late request fired by another
        test on the shared mock (#523 cross-test contamination).
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": t} for t in texts],
        match=match,
    )


def _configure_mock_tool_then_text(
    mock_llm_server_url: str,
    tool_calls: list[dict[str, str]],
    follow_up_text: str,
    *,
    match: str | None = None,
) -> None:
    """
    Configure a tool-call response followed by a text response.

    The first LLM call returns a function_call; after the tool
    executes and the result is sent back, the second LLM call
    returns a plain text reply.

    :param mock_llm_server_url: Mock server base URL.
    :param tool_calls: Tool call dicts (``call_id``, ``name``,
        ``arguments``).
    :param follow_up_text: Text for the second LLM call.
    :param match: Optional content-routing token (the unique user
        message this test sends). When set, these responses are served
        only to requests whose user input contains the token, isolating
        this test's queue from a stray/late request fired by another
        test on the shared mock (#523 cross-test contamination).
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": tool_calls},
            {"text": follow_up_text},
        ],
        match=match,
    )


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
    for that stale pattern times out even though the turn finished.

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
    mock_llm_server_url: str,
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
    _configure_mock_text(
        mock_llm_server_url, ["Hi there! How can I help you today?"], match="approve-llm-resp"
    )
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
        child.send("Hello approve-llm-resp" + "\r")
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
    mock_llm_server_url: str,
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
    # No LLM call expected on refuse — configure a dummy response
    # so the mock doesn't 500 if the server unexpectedly calls it.
    _configure_mock_text(mock_llm_server_url, ["should not appear"], match="deny-sentinel")
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

        child.send("Hello deny-sentinel" + "\r")
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
    mock_llm_server_url: str,
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
    # Two turns, each approved — two LLM responses needed.
    _configure_mock_text(
        mock_llm_server_url,
        [
            "Hello! Nice to meet you.",
            "Sure thing, got it!",
        ],
        match="two-turns-guard",
    )
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
        child.send("Hello two-turns-guard" + "\r")
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
    mock_llm_server_url: str,
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
    # Turn 1 approved-always, turn 2 auto-approved — two LLM calls.
    _configure_mock_text(
        mock_llm_server_url,
        [
            "Hello there!",
            "Following up as requested.",
        ],
        match="approve-cache",
    )
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
        child.send("Hello approve-cache" + "\r")
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
    mock_llm_server_url: str,
) -> None:
    """
    TOOL_CALL ASK → approve → tool runs → LLM responds.

    The mock LLM is scripted (like the TOOL_RESULT tests) to emit
    an ``echo`` ``function_call`` on the first call, then a plain
    text follow-up on the second. The ``ask_before_echo`` policy
    ASKs on every ``tool_call:echo``. After the user approves, the
    tool runs and its output (prefixed ``echo:``) flows back to the
    LLM in the follow-up call.

    The banner's ``phase`` field must be ``tool_call`` — not
    ``input`` — which is the critical distinction from the
    INPUT-phase tests above. Proves the TOOL_CALL site is
    wired and end-to-end correct.
    """
    follow_up = "tool-call-approve-followup-marker"
    _configure_mock_tool_then_text(
        mock_llm_server_url,
        [
            {
                "call_id": "tc1",
                "name": "echo",
                "arguments": json.dumps({"message": "testing123"}),
            }
        ],
        follow_up,
        match="testing123",
    )
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
        # Sync on the post-tool follow-up reply — it only renders after the
        # LLM's second call, proving the tool round-trip completed.
        child.expect(follow_up, timeout=120)
        # The echo tool runs; its output prefix 'echo:' should reach the
        # LLM's function_call_output on the follow-up call.
        joined = _wait_for_function_call_outputs(mock_llm_server_url)
        assert "echo: testing123" in joined, (
            "Tool output did not reach the LLM's function_call_output after "
            f"approval.\nfunction_call_outputs: {joined[:800]}"
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
    mock_llm_server_url: str,
) -> None:
    """
    TOOL_CALL ASK → refuse → tool NEVER runs → sentinel
    replaces output → LLM sees sentinel and typically relays
    that denial to the user.

    Load-bearing: the raw tool output MUST NOT reach the
    conversation — ``_enforce_tool_result_policy`` substitutes
    ``[Denied by policy: ...]``. This test is the end-to-end
    proof that the pre-persistence ordering holds under real
    streaming + DBOS parking. The mock LLM is scripted to emit
    the ``echo`` ``function_call`` so the TOOL_CALL ASK fires.
    """
    follow_up = "tool-call-refuse-followup-marker"
    _configure_mock_tool_then_text(
        mock_llm_server_url,
        [
            {
                "call_id": "tc2",
                "name": "echo",
                "arguments": json.dumps({"message": "testing456"}),
            }
        ],
        follow_up,
        match="testing456",
    )
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
        # Sync on the post-tool follow-up reply — the LLM only makes its
        # second call after the (blocked) tool round-trip completes.
        child.expect(follow_up, timeout=120)
        # On a TOOL_CALL-phase refusal the tool never runs: its
        # function_call_output is a denial marker, NOT the raw echo
        # output. (The ``[Denied by policy: ...]`` sentinel is the
        # separate TOOL_RESULT substitution path.) The regression
        # guard: a denial is recorded AND the raw echo output must
        # NEVER reach the conversation.
        joined = _wait_for_function_call_outputs(mock_llm_server_url)
        assert "denied" in joined.lower(), (
            "Tool-call denial marker did not appear in the LLM's "
            "function_call_output — refusal enforcement may have "
            f"regressed.\nfunction_call_outputs: {joined[:800]}"
        )
        assert "echo: testing456" not in joined, (
            "Raw tool output leaked to the LLM despite refusal — the tool "
            f"ran when it must not have.\nfunction_call_outputs: {joined[:800]}"
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


def test_repl_subagent_ask_does_not_tunnel_banner_to_root(
    ap_cli: str,
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Sub-agent INPUT-phase ASK does NOT surface a banner on the root
    REPL — it is non-interactive today.

    This asserts the CURRENT behavior, not the aspirational tunneled
    interactive approval (which is tracked by #765). The original
    test expected the worker's INPUT ASK to tunnel a
    ``⚠ approval required`` banner onto the root SSE stream and wait
    for the user to approve. That interactive tunnel is not wired in
    the REPL path.

    Observed reality (verified live against the mock LLM): the parent
    spawns the ``worker`` sub-agent via ``sys_session_send``; the
    worker's ``worker_input_gate`` (an ``on: [request]`` ASK) does
    NOT park for a root-surfaced prompt — the headless sub-agent runs
    to completion, its reply lands in the parent's inbox, and the
    parent composes its final summary. The turn finishes without any
    banner or human interaction.

    Load-bearing assertions:

    - NO ``approval required`` banner surfaces on the root REPL.
    - The parent's final summary text appears, proving the sub-agent
      ran end-to-end despite the unprompted ASK.
    """
    # The parent (root user message) and the worker (delegated task)
    # draw from separate content-routed queues on DISTINCT,
    # mutually-non-substring tokens so each agent's scripted calls land
    # deterministically. With a single shared queue the parent's
    # post-spawn continuation call races the worker's call: the parent
    # consumes the worker's queued reply and the worker parks forever
    # (the flake). Splitting them closes that intra-test race and the
    # model-fallback contamination vector entirely (#523 isolation):
    #   - "saask-parent" appears ONLY in the root user message, so the
    #     parent's calls route here. The delegated-task token lives in a
    #     function_call, not user content, so it never leaks into the
    #     parent's user text.
    #   - "saask-worker" appears ONLY in the task delegated to the
    #     worker, so the sub-agent's call routes to its own queue.
    worker_reply = "worker-reply-render-marker"
    parent_summary = "parent-summary-render-marker"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "sa1",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "worker", "title": "t", "args": "say hello saask-worker"}
                        ),
                    }
                ]
            },
            {"text": parent_summary},
            {"text": "(spare)"},
            {"text": "(spare)"},
        ],
        match="saask-parent",
    )
    # Worker queue — content-routed on the delegated-task token.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": worker_reply},
            {"text": "(spare)"},
            {"text": "(spare)"},
        ],
        match="saask-worker",
    )
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
        child.send("say hello saask-parent" + "\r")
        # Deterministic content-marker sync: the parent's summary renders
        # only after the worker ran to completion and its result landed in
        # the inbox. Had the worker parked on the unprompted ASK, the
        # result would never arrive and this would never render. Keying on
        # the marker (not the racy ``· ready`` toolbar) makes it stable.
        child.expect(parent_summary, timeout=120)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # Drain any trailing render so a late line is captured.
        full_turn += _read_pending(child, seconds=2.0)
        # No interactive approval banner tunneled to the root REPL.
        assert "approval required" not in full_turn, (
            "A sub-agent ASK banner surfaced on the root REPL — interactive "
            "tunneled mid-flight ASK is not implemented (see #765); the "
            f"sub-agent ASK is non-interactive today.\nCaptured:\n{full_turn[:1500]}"
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
    mock_llm_server_url: str,
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
    # Turn 1: LLM responds normally (no ASK). Turn 2: ASK fires,
    # approved, then LLM responds.
    _configure_mock_text(
        mock_llm_server_url,
        [
            "Got it, banana trigger noted.",
            "Continuing as requested.",
        ],
        match="label-approve",
    )
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
        child.send("hello BANANA_TRIGGER label-approve" + "\r")
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
    mock_llm_server_url: str,
) -> None:
    """
    Same composition, refuse path.

    Turn 2's ASK refused → server substitutes the DENY
    sentinel → REPL shows ``Denied by policy``. Proves the
    label-gated ASK's refuse branch goes through the same
    pre-persist sentinel path as INPUT DENY.
    """
    # Turn 1: LLM responds normally. Turn 2: refused — DENY sentinel,
    # no second LLM call. Extra dummy response as fail-safe.
    _configure_mock_text(
        mock_llm_server_url,
        [
            "Banana trigger received.",
            "should not appear",
        ],
        match="label-refuse",
    )
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
        child.send("hi BANANA_TRIGGER label-refuse" + "\r")
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


def test_repl_output_ask_does_not_prompt_in_repl(
    ap_cli: str,
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    RESPONSE(OUTPUT)-phase ASK is NON-INTERACTIVE in the REPL today.

    Asserts the CURRENT behavior, not an aspirational one. Like the
    TOOL_RESULT phase (#775), a RESPONSE-phase ASK does NOT surface a
    ``⚠ approval required`` banner: the ``ask_on_output`` policy fires
    but cannot prompt mid-flight, so the assistant reply passes straight
    through to the user and the turn completes normally. Interactive
    mid-flight ASK is tracked by #765.

    Verified live against the mock LLM: ``say hi`` → the scripted reply
    renders (``◆ <reply>``) and the turn settles to ``ready`` with NO
    banner and NO deny sentinel.

    (History: this test previously asserted an interactive *approve* path
    and was ``pytest.skip``-ped as "requires real LLM" — both were
    misdiagnoses. ``repl_env`` already targets the mock server, and
    RESPONSE-phase ASK is a pass-through, not an interactive gate.
    Contrast #789, which proved TOOL_CALL *does* surface a banner; the
    "same fix applies to OUTPUT" follow-up there does not hold.)
    """
    reply = "output-passthrough-reply-marker"
    _configure_mock_text(mock_llm_server_url, [reply], match="output-noprompt")
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
        child.send("say hi output-noprompt" + "\r")
        # The reply renders without ever parking on a banner. Sync on the
        # reply marker (deterministic content) rather than the cosmetic
        # `· ready` idle-settle, which can race/not-render under CI load.
        child.expect(reply, timeout=120)
        full_turn = _strip_ansi((child.before or "") + reply)
        # NO interactive approval banner for the RESPONSE-phase ASK.
        assert "approval required" not in full_turn, (
            "A RESPONSE-phase approval banner surfaced — interactive output "
            "ASK is not implemented (it is a pass-through today; see #765); "
            f"the REPL must not render one.\nCaptured:\n{full_turn[:1500]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_output_ask_passes_reply_through_no_sentinel(
    ap_cli: str,
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    RESPONSE(OUTPUT)-phase ASK passes the reply through UNCHANGED.

    Security-relevant companion to
    :func:`test_repl_output_ask_does_not_prompt_in_repl`: not only is
    there no banner, the raw reply is also NOT replaced by a
    ``[Denied by policy: ...]`` sentinel — the ASK verdict is a silent
    no-op, so the model's output reaches the user verbatim. This is a
    **fail-OPEN** gate, the same shape as TOOL_RESULT ASK (#775) and the
    opposite of a DENY. (Contrast the INPUT and TOOL_CALL phases, which
    DO surface an interactive banner — #789.) The missing interactive
    enforcement is tracked by #765.

    (History: this test previously asserted a *refuse → sentinel* path
    and was ``pytest.skip``-ped as "requires real LLM". Both were
    misdiagnoses: ``repl_env`` targets the mock server, and there is no
    interactive refuse — the ASK passes through, so no sentinel is
    substituted. Verified live against the mock.)
    """
    reply = "output-verbatim-reply-marker"
    _configure_mock_text(mock_llm_server_url, [reply], match="output-passthru")
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
        child.send("say hi output-passthru" + "\r")
        # The raw reply reaches the user verbatim (no DENY substitution).
        # Sync on the reply marker itself — its appearance IS the proof the
        # output passed through ungated.
        child.expect(reply, timeout=120)
        full_turn = _strip_ansi((child.before or "") + reply)
        # Fail-open: NO deny sentinel replaced the reply (RESPONSE-phase ASK
        # is a no-op today, not a block).
        assert "Denied by policy" not in full_turn, (
            "A deny sentinel replaced the reply — RESPONSE-phase ASK does not "
            f"collapse to DENY on this path today (see #765).\nCaptured:\n{full_turn[:1500]}"
        )
        # And no interactive banner either.
        assert "approval required" not in full_turn, (
            "A RESPONSE-phase approval banner surfaced — interactive output "
            f"ASK is not implemented (see #765).\nCaptured:\n{full_turn[:1500]}"
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


def test_repl_tool_result_ask_does_not_prompt_in_repl(
    ap_cli: str,
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    TOOL_RESULT-phase ASK is NON-INTERACTIVE in the REPL today.

    This asserts the CURRENT behavior, not an aspirational one.
    Interactive mid-flight ASK is tracked by #765; until then a
    TOOL_RESULT ASK never surfaces a ``⚠ approval required``
    banner in the REPL.

    Observed reality (verified live against the mock LLM): the
    ``ask_on_echo_result`` policy fires at the TOOL_RESULT phase but
    cannot prompt mid-flight, so the tool's output is NOT held for
    review — it passes straight through to the LLM and the turn
    completes normally. (The runner-side collapse-to-DENY helper in
    ``policy.py`` is currently unused on this server-mediated
    callable-tool path; the server's TOOL_RESULT enforcement at
    ``sessions.py`` acts only on DENY/transform verdicts, so an ASK
    verdict is a pass-through.) The net REPL outcome:

    - NO approval banner appears.
    - The tool runs and its ``echo: <input>`` output reaches the LLM.
    - The mock follow-up text lands as the assistant reply.

    Load-bearing assertions: (1) no banner, (2) the raw tool output
    reached the LLM's function_call_output (no deny sentinel
    substituted), proving the ASK was a no-op rather than a block.
    """
    follow_up = "tool-result-followup-marker"
    _configure_mock_tool_then_text(
        mock_llm_server_url,
        [
            {
                "call_id": "tr1",
                "name": "echo",
                "arguments": json.dumps({"message": "pineapple"}),
            }
        ],
        follow_up,
        match="pineapple",
    )
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
        # Sync on the follow-up reply, NOT the cosmetic `· ready` idle-settle.
        # The post-tool reply only renders after the LLM's second call (the
        # tool round-trip completed) and proves the turn finished — while the
        # `· ready` idle-settle marker intermittently fails to render under CI
        # load (the #523 pexpect family), hanging the wait past 120s even
        # though the turn itself completed. The sibling pass-through test syncs
        # the same way and is stable. If a banner had parked the turn, the
        # follow-up would never arrive and this expect would time out.
        child.expect(follow_up, timeout=120)
        full_turn = _strip_ansi((child.before or "") + follow_up)
        # No interactive approval banner for the TOOL_RESULT ASK.
        assert "approval required" not in full_turn, (
            "A TOOL_RESULT-phase approval banner surfaced — interactive "
            "mid-flight ASK is not implemented (see #765); the REPL must "
            f"not render one today.\nCaptured:\n{full_turn[:1500]}"
        )
        # The raw tool output reached the LLM untouched (no deny
        # sentinel) — the ASK passed through rather than blocking.
        joined = _wait_for_function_call_outputs(mock_llm_server_url)
        assert "echo: pineapple" in joined, (
            "Expected the raw echo output to reach the LLM's "
            "function_call_output (TOOL_RESULT ASK is a no-op today), but "
            f"it was not present.\nfunction_call_outputs: {joined[:800]}"
        )
        assert "Denied by policy" not in joined, (
            "A deny sentinel replaced the tool output — the current "
            "TOOL_RESULT ASK path does not collapse to DENY on this "
            f"callable-tool path.\nfunction_call_outputs: {joined[:800]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


def test_repl_tool_result_ask_passes_output_through(
    ap_cli: str,
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    TOOL_RESULT-phase ASK cannot be refused interactively today.

    Companion to ``test_repl_tool_result_ask_does_not_prompt_in_repl``
    from the "refuse" angle. The original test expected the user to
    refuse the tool result and see a ``[Denied by policy: ...]``
    sentinel. That interactive refuse is not implemented (tracked by
    #765): with no banner, there is nothing for the user to refuse,
    so the tool output is never suppressed.

    This asserts the CURRENT behavior: a TOOL_RESULT ASK leaves the
    tool output intact (no sentinel) and the turn completes. We pin
    the second turn explicitly — a stale-state regression that DID
    start suppressing output would surface a sentinel here.
    """
    follow_up = "second-turn-followup-marker"
    _configure_mock_tool_then_text(
        mock_llm_server_url,
        [
            {
                "call_id": "tr2",
                "name": "echo",
                "arguments": json.dumps({"message": "mangosteen"}),
            }
        ],
        follow_up,
        match="mangosteen",
    )
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
        # Sync on the post-tool follow-up reply: it only renders after the
        # LLM's second call, which requires the function_call_output round-trip
        # to have completed. 120s headroom for REPL turn latency under CI
        # worker contention (#523 family); within the --timeout=180 pytest cap.
        # (The follow-up render is a proxy; the function_call_output assertion
        # below waits on the real signal via _wait_for_function_call_outputs,
        # which polls until the mock has recorded the output.)
        child.expect(follow_up, timeout=120)
        full_turn = _strip_ansi((child.before or "") + follow_up)
        assert "approval required" not in full_turn, (
            "A TOOL_RESULT-phase approval banner surfaced — interactive "
            f"mid-flight ASK is not implemented (see #765).\nCaptured:\n{full_turn[:1500]}"
        )
        # No deny sentinel: today's path does not suppress the output.
        joined = _wait_for_function_call_outputs(mock_llm_server_url)
        assert "echo: mangosteen" in joined, (
            "The raw echo output did not reach the LLM — TOOL_RESULT ASK "
            f"is a no-op today and must not suppress it.\noutputs: {joined[:800]}"
        )
        assert "Denied by policy" not in joined, (
            "A deny sentinel appeared — the TOOL_RESULT ASK refuse path is "
            f"not wired in the REPL today (see #765).\noutputs: {joined[:800]}"
        )
    finally:
        try:
            child.send("/quit" + "\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)


# ── Sub-agent TOOL_CALL approval (non-interactive) ─────────
#
# The sub-agent fires an ASK from the TOOL_CALL phase. Like the
# INPUT-phase sub-agent ASK, it does NOT tunnel an interactive
# banner to the root REPL today — the headless sub-agent runs
# the tool to completion and the ASK is a pass-through (#765).


def test_repl_subagent_tool_call_ask_does_not_tunnel_banner_to_root(
    ap_cli: str,
    repl_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Sub-agent TOOL_CALL ASK does NOT tunnel a banner to the root REPL.

    This asserts the CURRENT behavior, not the aspirational tunneled
    interactive approval (tracked by #765). The fixture's
    ``toolworker`` sub-agent has a LOCAL function tool ``echo`` and a
    ``worker_tool_gate`` policy that ASKs on ``tool_call:echo``.

    Observed reality (captured live against the mock LLM): the parent
    spawns ``toolworker`` via ``sys_session_send``; the headless
    sub-agent's ``echo`` TOOL_CALL ASK does NOT park for a
    root-surfaced prompt — the sub-agent runs ``echo`` to completion,
    its reply lands in the parent's inbox, and the parent composes its
    final summary. The turn finishes with no banner or human
    interaction.

    Load-bearing assertions:

    - The sub-agent's nested local ``echo`` tool registers with the
      spawned child's executor — the call does NOT error
      ``Tool echo not found`` (the regression in #763).
    - NO ``approval required`` banner surfaces on the root REPL.
    - The parent's final summary renders, proving the sub-agent ran
      end-to-end (echo executed) despite the unprompted TOOL_CALL ASK.

    The parent (``model: gpt-4o``) and the sub-agent
    (``model: gpt-4o-mini``) draw from separate keyed mock queues so
    each agent's scripted calls land deterministically — a single
    shared queue races the parent's continuation call against the
    sub-agent's ``echo`` call (the parent would consume the
    sub-agent's tool call and error ``Tool echo not found``).
    """
    worker_reply = "worker-reply-render-marker"
    parent_summary = "parent-summary-render-marker"
    reset_mock_llm(mock_llm_server_url)
    # Both queues are content-routed on DISTINCT, mutually-non-substring
    # tokens so parent and sub-agent calls split correctly AND neither
    # queue is reachable by model fallback (closes the gpt-4o / gpt-4o-mini
    # contamination vectors entirely — #523 isolation):
    #   - "statool-parent" appears ONLY in the root user message, so the
    #     parent's calls (and its post-spawn continuation) route here. The
    #     delegated-task token lives in a function_call, not user content,
    #     so it never leaks into the parent's user text.
    #   - "statool-worker" appears ONLY in the task delegated to the
    #     toolworker, so the sub-agent's calls route to its own queue.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "sa1",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "toolworker",
                                "title": "t",
                                "args": "return the word durian statool-worker",
                            }
                        ),
                    }
                ]
            },
            {"text": parent_summary},
            {"text": "(spare)"},
            {"text": "(spare)"},
        ],
        match="statool-parent",
    )
    # Toolworker queue — content-routed on the delegated-task token.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "we1",
                        "name": "echo",
                        "arguments": json.dumps({"message": "durian"}),
                    }
                ]
            },
            {"text": worker_reply},
            {"text": "(spare)"},
            {"text": "(spare)"},
        ],
        match="statool-worker",
    )
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
        child.send("return the word durian statool-parent" + "\r")
        # Deterministic content-marker sync: the parent's summary text
        # renders only after the sub-agent ran echo end-to-end and its
        # result landed in the inbox. Keying on the marker (not the
        # racy ``· ready`` toolbar) makes the completion signal stable.
        child.expect(parent_summary, timeout=120)
        full_turn = child.before or ""
        if isinstance(full_turn, bytes):
            full_turn = full_turn.decode("utf-8", errors="replace")
        full_turn = _strip_ansi(full_turn)
        # Drain any trailing render so a late line is captured.
        full_turn += _read_pending(child, seconds=2.0)
        # The nested local echo tool registered with the spawned
        # child's executor — the SDK did NOT reject the call. This is
        # the #763 regression guard.
        assert "Tool echo not found" not in full_turn, (
            "The sub-agent's nested local 'echo' tool failed to register "
            "with the spawned child's executor (regression #763).\n"
            f"Captured:\n{full_turn[:1500]}"
        )
        # No interactive approval banner tunneled to the root REPL —
        # the sub-agent TOOL_CALL ASK is a non-interactive pass-through
        # today (see #765), exactly like the sub-agent INPUT-phase ASK.
        assert "approval required" not in full_turn, (
            "A sub-agent TOOL_CALL ASK banner surfaced on the root REPL — "
            "interactive tunneled mid-flight ASK is not implemented (see "
            "#765); the sub-agent ASK is non-interactive today.\n"
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
