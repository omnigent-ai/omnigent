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

    1. Initial ``/model`` renders the ``Active:`` readout with no
       in-session override (``no model pinned``).
    2. ``/model <name>`` confirms ``model set to <name>``.
    3. Subsequent ``/model`` shows ``<name>`` in the ``Active:`` readout
       (session override persisted).
    4. ``/model default`` confirms ``model reset to agent default``.

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

        # No-arg /model renders the active-credential readout:
        #   "Active:  <model | (no model pinned …)>  ·  <provider>  ·  <source>"
        # (this replaced the legacy "model: (agent default)" line; see
        # _build_model_readout_lines in omnigent/repl/_repl.py). With no
        # in-session override yet, the model slot reads "no model pinned" —
        # ``--model`` sets the routing model, not the /model session override
        # the readout tracks (``session.model_override``).
        _submit_slash_command(child, "/model")
        child.expect("Active:", timeout=10)
        child.expect("no model pinned", timeout=10)

        _submit_slash_command(child, f"/model {_OVERRIDE_MODEL}")
        # Set confirmation: "model set to <name> for future responses".
        child.expect(f"model set to {_OVERRIDE_MODEL}", timeout=10)

        # Nudge a fresh prompt to confirm the input buffer is clear before
        # the next slash command.
        child.send("\r")
        child.expect(r"❯ ", timeout=10)

        # After the set, the readout's model slot shows the override (prior
        # occurrences — the set confirmation — are already consumed, so the
        # next match is the fresh readout line).
        _submit_slash_command(child, "/model")
        child.expect("Active:", timeout=10)
        child.expect(_OVERRIDE_MODEL, timeout=10)

        _submit_slash_command(child, "/model default")
        child.expect("model reset to agent default", timeout=10)

        clean_exit(child, timeout=_EXIT_TIMEOUT)
        assert child.exitstatus in (0, None)
        assert child.signalstatus is None
    finally:
        if not child.closed:
            child.close(force=True)
