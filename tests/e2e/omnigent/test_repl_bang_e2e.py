"""End-to-end coverage for the REPL "!" shell passthrough.

The unit suite (``tests/repl/test_bang_command.py``) covers the extracted
helpers — the clip/context builders and the ``_run_bang_command`` runner. What
it cannot reach is the wiring inside ``run_repl.on_input``: the ``!`` / ``!!`` /
bare-``!`` dispatch and the buffer that folds a command's output into the *next*
agent turn. Those only exist as a closure over the live REPL, so they are
exercised here by driving the real ``omnigent run`` REPL under a PTY and
inspecting what the model actually received via the mock LLM server.

**What breaks if these fail:**
- ``!<cmd>`` stops running in the shell / stops rendering its output + footer.
- A bare ``!<cmd>`` wrongly costs a model turn (or silently drops its output
  instead of folding it into the next turn's ``llm_text``).
- The bare ``!`` usage hint or the ``!!`` literal-escape routing regresses.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_MODEL = "mock-model"
_HARNESS = "openai-agents"

# Visible turn-synchronization markers — same ones the green smoke / model /
# effort e2e tests use. ``working`` is the streaming activity line; ``❯ `` is
# the input prompt the REPL re-renders when a turn settles.
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_BANG_TIMEOUT = 20.0
_EXIT_TIMEOUT = 15.0


def _captured_requests(mock_llm_server_url: str) -> list[dict]:
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=5.0)
    resp.raise_for_status()
    return resp.json()["requests"]


def test_repl_bang_runs_and_folds_into_next_turn(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``!echo`` runs locally, renders its output, costs no model turn, and its
    output is folded into the following prompt's request to the model.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": "Understood."}])
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)

        # A "!" command runs in the shell and prints its own exit footer; it
        # does NOT enter a model turn, so synchronize on the footer, not on
        # "working".
        submit_prompt(child, "!echo BANG_SENTINEL_XYZ")
        child.expect(r"exit 0", timeout=_BANG_TIMEOUT)
        bang_render = strip_ansi(child.before or "") + strip_ansi(drain_for(child, 2.0))
        assert "BANG_SENTINEL_XYZ" in bang_render, (
            "bang command stdout not rendered on screen:\n" + bang_render[-1500:]
        )

        # The bang alone must not have hit the model.
        assert _captured_requests(mock_llm_server_url) == [], (
            "a bare '!' command wrongly triggered a model request"
        )

        # Now a normal prompt: the buffered bang output must be folded into it.
        submit_prompt(child, "what did that print?")
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    requests = _captured_requests(mock_llm_server_url)
    assert requests, "the follow-up prompt never reached the model"
    blob = json.dumps(requests)
    assert "BANG_SENTINEL_XYZ" in blob, (
        "bang stdout was not folded into the next turn's model request:\n" + blob[-2000:]
    )
    assert "I ran a shell command" in blob, (
        "the model-facing bang context header is missing from the request"
    )
    assert "what did that print?" in blob, (
        "the user's follow-up prompt is missing from the model request"
    )


def test_repl_bang_bare_shows_hint_and_double_bang_escapes(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    A bare ``!`` prints a usage hint and costs no model turn; ``!!text`` drops
    one ``!`` and is sent to the model as the literal prompt ``!text`` (it is
    NOT executed as a shell command).
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": "Ok."}])
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)

        submit_prompt(child, "!")
        child.expect(r"runs a shell command", timeout=_BANG_TIMEOUT)
        assert _captured_requests(mock_llm_server_url) == [], (
            "a bare '!' usage hint wrongly triggered a model request"
        )

        # "!!" escapes: one "!" is stripped and the rest is an ordinary prompt.
        submit_prompt(child, "!!literal-bang-prompt")
        turn = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    requests = _captured_requests(mock_llm_server_url)
    assert requests, "the '!!'-escaped prompt never reached the model"
    blob = json.dumps(requests)
    # Exactly one leading "!" survives the escape.
    assert "!literal-bang-prompt" in blob, (
        "'!!' did not send a literal single-'!' prompt to the model:\n" + blob[-2000:]
    )
    # The escaped prompt must NOT have been run as a shell command.
    assert "I ran a shell command" not in blob, (
        "'!!' prompt was wrongly executed as a shell command"
    )
    assert "exit 0" not in turn.stripped, (
        "'!!' prompt rendered a shell exit footer — it was executed, not escaped"
    )


def test_repl_bang_buffer_dropped_on_new_conversation(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Buffered ``!`` output is discarded when a new conversation starts (``/clear``)
    — it belonged to the prior conversation and must not leak into the fresh
    conversation's first turn.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": "Ok."}])
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)

        submit_prompt(child, "!echo LEAK_SENTINEL_QQQ")
        child.expect(r"exit 0", timeout=_BANG_TIMEOUT)

        # /clear starts a new conversation — it must drop the buffered output.
        # Re-sync on the freshly re-rendered prompt before sending the next
        # line, otherwise it races the /clear redraw and never submits.
        submit_prompt(child, "/clear")
        child.expect(_COMPLETION_MARKER, timeout=_BOOT_TIMEOUT)
        drain_for(child, 1.5)

        submit_prompt(child, "hello fresh conversation")
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    requests = _captured_requests(mock_llm_server_url)
    assert requests, "the post-/clear prompt never reached the model"
    blob = json.dumps(requests)
    assert "LEAK_SENTINEL_QQQ" not in blob, (
        "buffered bang output leaked into the new conversation after /clear:\n" + blob[-2000:]
    )
    assert "hello fresh conversation" in blob, (
        "the post-/clear prompt is missing from the model request"
    )
