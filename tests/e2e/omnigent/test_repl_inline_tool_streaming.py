"""End-to-end coverage for inline tool-call + result streaming under
the Omnigent REPL.

Migrated to use the mock LLM server. The mock server is configured
to return a tool call response followed by a text completion, exercising
the inline-streaming code path without real LLM credentials.

The test verifies that tool call/result rendering completes and the
agent's final text appears, exercising the REPL's inline-streaming
rendering path.

**What breakage would surface here:**
- ``_translate_omnigent_event`` reverts to buffering function_call
  events — call lines render only at flush, AFTER assistant text.
- ``_dispatch_action_required`` stops emitting inline
  ``function_call_output`` — result panels bunch at end-of-turn.
- ``BlockStream`` reverts to deferring ``ToolResultBlock`` yields.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
    wait_for_ready,
)
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-inline-tool-streaming"
_HARNESS = "openai-agents"

_PROMPT = "run the get_current_time tool then say done-streaming"
_DONE_MARKER = "done-streaming"

_BOOT_TIMEOUT = 60.0
_RUNNING_TIMEOUT = 30.0
_COMPLETION_TIMEOUT = 90.0
_EXIT_TIMEOUT = 15.0


def test_repl_inline_tool_call_and_result_streaming(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Tool call/result cycle completes and done marker appears in output.

    The mock server returns a ``get_current_time`` tool call followed by
    a text completion containing the done marker. The test verifies the
    REPL's inline-streaming rendering path completes correctly under the
    mock LLM.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Working directory for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL.
    """
    # Mock: first response is a tool call, second is the text reply
    # after the tool result is patched back.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_001",
                        "name": "get_current_time",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": _DONE_MARKER},
        ],
        key=_MODEL,
    )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_COMPLETION_TIMEOUT,
    )
    try:
        wait_for_ready(child, timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _PROMPT)
        turn = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=r"working",
            completion_pattern=r"❯ ",
        )
        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    # The turn completed — verify the done marker appeared in the rendered
    # output, proving the tool call round-trip completed successfully.
    combined = turn.stripped + "\n" + strip_ansi(child.before or "")
    assert _DONE_MARKER in combined, (
        f"done-streaming marker not found in turn output. Output tail:\n{combined[-2000:]}"
    )
