"""E2E: /model command in the Omnigent REPL under pexpect.

Migrated to mock LLM: drives ``/model`` against a mock ``omnigent run``
REPL and asserts the slash-command surface — show / set / show-after-set
/ reset — matches the design's contract end-to-end. No real Databricks
credentials required.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
)
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-repl-model"
_HARNESS = "openai-agents"
# Override target. Any non-empty model id distinct from the spawn one
# is sufficient for the show/set assertions.
_OVERRIDE_MODEL = "mock-repl-model-override"
_SPAWN_TIMEOUT = 90.0
_BOOT_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def _submit_slash_command(child, text: str) -> None:  # type: ignore[no-untyped-def]
    """Submit a slash command under prompt-toolkit/pexpect."""
    submit_prompt(child, text)


def test_repl_model_command_show_set_reset(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """Drive /model through its full state machine in the REPL.

    Uses the mock LLM server so no real Databricks credentials are
    needed. The /model slash commands are handled entirely within the
    REPL process — no LLM turn is required to test the UI state machine.

    Asserts each transition:

    1. Initial ``/model`` echoes ``(agent default)``.
    2. ``/model <name>`` confirms ``model set to <name>``.
    3. Subsequent ``/model`` echoes the override (session state persisted).
    4. ``/model default`` confirms reset to ``agent default``.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Working directory for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL.
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
        # Match the visible prompt marker rather than the bottom-
        # toolbar state text: under pexpect the prompt-toolkit CPR
        # handshake can suppress ``state: sleeping`` even when the
        # REPL is ready.
        child.expect(r"❯ ", timeout=_BOOT_TIMEOUT)

        _submit_slash_command(child, "/model")
        # ``(agent default)`` is the canonical phrase the no-override
        # show branch emits — search the slash-command handler for
        # the matching string if you rename it.
        child.expect(r"model: \(agent default\)", timeout=10)
        # Usage hint is always printed alongside the show line.
        child.expect("usage: /model", timeout=10)

        _submit_slash_command(child, f"/model {_OVERRIDE_MODEL}")
        child.expect(f"model set to {_OVERRIDE_MODEL}", timeout=10)

        # Same drain trick test_repl_effort_e2e.py uses: nudge a
        # fresh prompt to confirm the input buffer is clear before
        # firing the next slash command.
        child.send("\r")
        child.expect(r"❯ ", timeout=10)

        _submit_slash_command(child, "/model")
        child.expect(f"model: {_OVERRIDE_MODEL}", timeout=10)

        _submit_slash_command(child, "/model default")
        child.expect("model reset to agent default", timeout=10)

        clean_exit(child, timeout=_EXIT_TIMEOUT)
        assert child.exitstatus in (0, None)
        assert child.signalstatus is None
    finally:
        if not child.closed:
            child.close(force=True)
