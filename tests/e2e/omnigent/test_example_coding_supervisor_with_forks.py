"""End-to-end test for ``examples/agents/coding_supervisor_with_forks``.

Supervisor + two worker sub-agents, each with a forked os_env
(hardlink-tree COW). Parametrized across all wrapped harnesses so
each one drives both the supervisor and its forked workers.

YAML has ``sandbox: type: none`` everywhere so the sandbox is
off; the fork mode itself works cross-platform.

**What breaks if this fails:**
- Sub-agent ``os_env.fork`` propagation regresses.
- Per-worker harness specification is lost during spec translation.
- The ``sys_session_*`` + forked-env combination stops wiring
  the symlinks under ``.sessions/<worker>/`` that the supervisor
  reads to diff worker output.
"""

from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from tests.e2e._harness_probes import HARNESS_HARNESS_MODELS, HARNESS_IDS
from tests.e2e.omnigent._example_helpers import (
    assert_completed_one_shot,
    require_claude_sdk,
    require_codex_cli,
    run_one_shot,
)
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_coding_supervisor_with_forks_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    harness: str,
    model: str,
) -> None:
    """
    Run the forked coding-supervisor one-shot across all wrapped harnesses
    using the mock LLM server.

    The CLI's ``--harness`` / ``--model`` flags override every executor
    block in the YAML so the parametrized harness drives both the
    supervisor and its forked workers.

    Harnesses that require a CLI binary (``claude-sdk``, ``codex``,
    ``pi``) skip loudly when their binary is absent from PATH — the
    mock LLM intercepts the API calls but the harness binary itself
    must be present to launch.  ``openai-agents`` is pure-Python and
    never skips.

    :param omnigent_python: Interpreter with omnigent + the harness's
        SDK installed.
    :param omnigent_repo_root: Repo root for subprocess cwd.
    :param mock_credentials_env: Env with ``OPENAI_BASE_URL`` /
        ``ANTHROPIC_BASE_URL`` pointing at the mock LLM server.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    :param harness: The harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: Unused — replaced by a per-harness mock key below.
        The real model from :data:`HARNESS_HARNESS_MODELS` would put
        the ``pi`` harness into gateway mode (pi inspects the
        ``databricks-*`` model name and switches to real-gateway auth,
        ignoring the mock's ``OPENAI_BASE_URL``); a ``mock-*`` key keeps
        every harness routed through the mock LLM server.
    """
    del model  # replaced by mock_model below
    if harness == "claude-sdk":
        require_claude_sdk()
        if which("claude") is None:
            pytest.skip(
                "claude-sdk harness prerequisite missing: the 'claude' "
                "CLI binary must be installed on PATH."
            )
    elif harness == "codex":
        require_codex_cli()
    elif harness == "pi":
        if which("pi") is None:
            pytest.skip("pi harness prerequisite missing: 'pi' CLI not on PATH.")

    # Per-harness mock key so concurrent harness rows get isolated mock
    # response queues, and so ``pi`` stays in mock mode rather than
    # gateway-routing a ``databricks-*`` model name.
    mock_model = f"mock-coding-supervisor-{harness}"
    # Pre-seed the mock queue with enough canned replies to cover the
    # supervisor turn plus both worker sub-agent turns and any auto-wake.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "I have delegated the work to the workers. Task complete."},
            {"text": "Worker A finished."},
            {"text": "Worker B finished."},
            {"text": "Both workers done. Summary: OK."},
        ],
        key=mock_model,
    )
    result = run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=mock_credentials_env,
        example_name="coding_supervisor_with_forks",
        harness=harness,
        model=mock_model,
    )
    assert_completed_one_shot(result, "coding_supervisor_with_forks")
