"""End-to-end test for ``examples/claude_code_agent.yaml``.

The example pins ``executor.type: claude_sdk`` and exposes
Claude Code's built-in tools (Bash, Read, Edit, etc.) plus any
Omnigent tools declared in YAML (passed through as MCP tools).

**What breaks if this fails:**
- ``executor.type: claude_sdk`` spec translation regresses.
- The ``claude_sdk`` harness wiring loses its MCP-tool bridging
  (Omnigent tools declared in YAML stop reaching Claude).
- Harness-specific ``--model`` resolution changes.

Dependency: requires the ``claude-agent-sdk`` Python package. We
fail loud upfront via :func:`require_claude_sdk` rather than
letting the subprocess die with a mid-run ImportError.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._example_helpers import (
    assert_completed_one_shot,
    require_claude_sdk,
    run_one_shot,
)


def test_claude_code_agent_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Run the claude_code_agent YAML one-shot through the claude_sdk
    harness (pinned in the YAML — we pass ``harness=None`` so the
    spec wins).

    Uses the mock LLM server so no real Anthropic credentials are
    needed.  ``ANTHROPIC_BASE_URL`` is injected into the env dict so
    the Claude SDK harness routes its ``POST /v1/messages`` calls to
    the mock server instead of api.anthropic.com.

    :param omnigent_python: Interpreter with omnigent +
        claude-agent-sdk installed.
    :param omnigent_repo_root: Repo root for subprocess cwd.
    :param mock_credentials_env: Env dict pointing at the mock LLM
        server (``OPENAI_BASE_URL`` + ``OPENAI_API_KEY``).
    :param mock_llm_server_url: Base URL of the mock LLM server
        (no ``/v1`` suffix — the Anthropic SDK appends
        ``/v1/messages`` automatically).
    """
    if shutil.which("claude") is None:
        pytest.skip("'claude' CLI is not on PATH. Install Claude Code to run this test.")

    require_claude_sdk()

    mock_model = "mock-claude-code-agent"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key=mock_model,
    )

    # Inject the Anthropic mock base URL so the Claude SDK harness routes
    # LLM calls to the mock server.  The SDK appends /v1/messages itself,
    # so we pass the raw server root without a /v1 suffix.
    env = dict(mock_credentials_env)
    env["ANTHROPIC_BASE_URL"] = mock_llm_server_url
    env["ANTHROPIC_API_KEY"] = "mock-key"
    env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] = "printf %s mock-key"

    result = run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=env,
        example_name="claude_code_agent",
        harness=None,  # Let the YAML's executor.type pin win.
        model=mock_model,
    )
    assert_completed_one_shot(result, "claude_code_agent")
