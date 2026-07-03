"""E2E: /effort command in the Omnigent REPL under pexpect.

Migrated to mock LLM: the test only exercises slash commands, so
no LLM turn is required; mock credentials suffice.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
)
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-effort-model"
_HARNESS = "openai-agents"
_SPAWN_TIMEOUT = 90.0
_BOOT_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def _submit_slash_command(child, text: str) -> None:  # type: ignore[no-untyped-def]
    """Submit a slash command under prompt-toolkit/pexpect."""
    submit_prompt(child, text)


def test_repl_effort_command_show_set_reset(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Drive /effort through its show / set / reset state machine.

    Uses the mock LLM server so no real LLM credentials are needed.
    The /effort slash commands are handled entirely within the REPL
    process — no LLM turn is required to assert the UI state machine.

    :param omnigent_python: Interpreter with omnigent +
        openai-agents installed.
    :param omnigent_repo_root: Working directory for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring queues.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "ok"}],
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
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        # Match the visible prompt marker rather than the bottom-toolbar
        # state text: under pexpect the prompt-toolkit CPR handshake can
        # suppress ``state: sleeping`` even though the REPL is ready.
        child.expect(r"❯ ", timeout=_BOOT_TIMEOUT)

        _submit_slash_command(child, "/effort")
        child.expect("reasoning effort: default", timeout=10)
        child.expect("options:", timeout=10)

        _submit_slash_command(child, "/effort high")
        child.expect("reasoning effort set to high", timeout=10)

        _submit_slash_command(child, "/effort")
        child.expect("reasoning effort: high", timeout=10)

        # Press Enter once to clear the previous slash command from the
        # prompt-toolkit input buffer. In this pexpect setup the command
        # output can arrive before the input widget has visually cleared,
        # so typing a normal prompt immediately can append to the old
        # slash-command text instead of starting a model turn.
        child.send("\r")
        child.expect(r"❯ ", timeout=10)

        _submit_slash_command(child, "/effort default")
        child.expect("reasoning effort reset to agent default", timeout=10)

        clean_exit(child, timeout=_EXIT_TIMEOUT)
        assert child.exitstatus in (0, None)
        assert child.signalstatus is None
    finally:
        if not child.closed:
            child.close(force=True)
