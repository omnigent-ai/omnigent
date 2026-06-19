"""End-to-end test for ``examples/agents/agent_with_os_env_fork``.

``agent_with_os_env`` + ``fork: true`` on the ``os_env:`` block.
Hardlink-tree COW gives the agent a private view of ``cwd``;
reads/writes/shell commands stay inside the fork.

The YAML has ``sandbox: type: none`` by default so the example
runs cross-platform — the fork mode itself works on both macOS
and Linux (hardlinks via ``os.link``). On Linux, enable
``linux_bwrap`` for an additional layer of write restriction.

**What breaks if this fails:**
- Spec parser regresses on ``os_env.fork: true``.
- The COW hardlink-tree setup in ``omnigent.inner.os_env``
  fails to create its private shadow directory.
- Shell commands' hardlink-break logic regresses, leaking writes
  back to the original cwd.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnigent._example_helpers import (
    assert_completed_one_shot,
    run_one_shot,
)
from tests.e2e.omnigent.conftest import configure_mock_llm


def test_agent_with_os_env_fork_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``omnigent run agent_with_os_env_fork -p <prompt>`` completes
    cleanly. The YAML points ``cwd: /tmp/fork-demo`` — the test
    creates that dir with one file so the fork has something to
    mirror.

    Uses the mock LLM server for deterministic responses.

    :param omnigent_python: Interpreter with omnigent +
        openai-agents installed.
    :param omnigent_repo_root: Repo root for subprocess cwd.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    # The YAML pins ``cwd: /tmp/fork-demo``. Create the dir with
    # a seed file so the fork has real content to hardlink
    # (rather than failing with FileNotFoundError at startup).
    fork_demo = Path("/tmp/fork-demo")
    fork_demo.mkdir(exist_ok=True)
    (fork_demo / "notes.txt").write_text("original content\n")

    configure_mock_llm(mock_llm_server_url, [{"text": "OK"}])
    result = run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=mock_credentials_env,
        example_name="agent_with_os_env_fork",
        model="mock-model",
    )
    assert_completed_one_shot(result, "agent_with_os_env_fork")
