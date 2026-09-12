"""Structural test for the PipesHub example (examples/pipeshub).

PipesHub is a single-agent recipe that answers questions over an
organization's PipesHub-connected knowledge sources (Slack, Drive,
Confluence, etc.) through PipesHub's MCP server, wired via a directory
``tools/mcp/pipeshub.yaml`` config. Pure spec-load — no LLM, no MCP
server, no live PipesHub deployment (``expand_env=False`` so the
``${PIPESHUB_MCP_URL}`` / ``${PIPESHUB_MCP_TOKEN}`` refs don't need to
resolve).

What breaks if this fails:
- the recipe silently pins a model (re-coupling it to one provider),
- the harness drifts off ``claude-sdk`` (the recipe is deliberately pinned
  to it — see the config.yaml comment on why: the MCP failure/runner-path
  story this example documents only applies to the SDK-harness path),
- a sub-agent appears (this is deliberately a single agent, not an
  orchestrator, matching ``deep-research``'s "one agent + one MCP server"
  pattern rather than the Polly/Debby-class multi-agent examples),
- the ``pipeshub`` MCP server is dropped/renamed or stops being an HTTP
  (not stdio) connector,
- the ``url``/``Authorization`` header stop templating via ``${VAR}``
  (the whole point of the directory-config shape: safe to commit, no
  hardcoded endpoint or secret),
- ``AGENTS.md`` stops being picked up as instructions (a stray top-level
  ``prompt:`` in config.yaml would be silently ignored per the parser's
  ``instructions`` > ``prompt`` precedence — this asserts the file
  actually won that precedence, not just that it exists on disk).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_pipeshub.py -> repo root is 3 parents up.
_PIPESHUB_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "pipeshub"


@pytest.fixture(scope="module")
def pipeshub_spec() -> AgentSpec:
    """Load and validate the pipeshub bundle once for the module.

    ``expand_env=False`` so the structural tests run without a live
    ``PIPESHUB_MCP_URL`` / ``PIPESHUB_MCP_TOKEN`` in the environment.
    """
    return load(_PIPESHUB_BUNDLE, expand_env=False)


def test_pipeshub_name_and_harness(pipeshub_spec: AgentSpec) -> None:
    """
    The agent is named ``pipeshub`` and is deliberately pinned to the
    claude-sdk harness with no pinned model or profile — the MCP
    failure-visibility story the config.yaml comment references only
    covers the SDK-harness/runner path, not native harnesses.
    """
    assert pipeshub_spec.name == "pipeshub"
    assert pipeshub_spec.executor.config.get("harness") == "claude-sdk"
    assert pipeshub_spec.executor.model is None
    assert pipeshub_spec.executor.profile is None


def test_pipeshub_is_single_agent(pipeshub_spec: AgentSpec) -> None:
    """PipesHub is a single agent — no sub-agents, no delegation."""
    assert pipeshub_spec.sub_agents == []
    assert pipeshub_spec.tools.agents == []


def test_pipeshub_wires_the_mcp_server_with_templated_auth(pipeshub_spec: AgentSpec) -> None:
    """
    The PipesHub MCP server is an HTTP connector whose ``url`` and
    ``Authorization`` header both use ``${VAR}`` — the directory config
    must stay safe to commit with no hardcoded endpoint or secret.
    """
    assert [s.name for s in pipeshub_spec.mcp_servers] == ["pipeshub"]
    server = pipeshub_spec.mcp_servers[0]
    assert server.transport == "http"
    assert server.url == "${PIPESHUB_MCP_URL}"
    assert server.headers.get("Authorization") == "Bearer ${PIPESHUB_MCP_TOKEN}"


def test_pipeshub_instructions_come_from_agents_md(pipeshub_spec: AgentSpec) -> None:
    """
    ``AGENTS.md`` is auto-discovered as instructions (config.yaml has no
    ``prompt:`` key, so there is nothing for ``instructions:`` to lose
    precedence to — this asserts the file's content actually landed,
    not just that a file with that name exists on disk).
    """
    assert pipeshub_spec.instructions is not None
    assert "PipesHub research agent" in pipeshub_spec.instructions
    assert "Search before answering" in pipeshub_spec.instructions
