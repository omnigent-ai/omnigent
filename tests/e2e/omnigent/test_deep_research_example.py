"""Structural test for the deep-research example bundle (examples/deep-research).

deep-research is a single-agent web researcher whose web-research tools come
from the hosted **Keenable MCP server** (``tools/mcp/keenable.yaml`` →
``search_web_pages`` + ``fetch_page_content``), auto-discovered by the spec
parser with no ``tools:`` block. Pure spec-load — no LLM, no credentials, no
live MCP endpoint — modeled on ``test_example_scribe.py``.

The agent name ``deep-research`` has a hyphen (not a valid Python test-module
name), so this file is intentionally not named ``test_example_deep-research.py``;
the examples-coverage-sync guard is told about it via the ``deep-research`` entry
in ``_ALT_COVERED`` (same pattern as ``openai-coder``).

What breaks if this fails:
- the Keenable MCP server stops being discovered from ``tools/mcp/*.yaml`` (the
  agent would load without its search and fetch capabilities),
- the ``deep-research`` skill is dropped or renamed,
- the ``claude-sdk`` omnigent executor stops translating,
- an ``os_env`` block is added, which would expose local file and shell tools
  to an agent intended to research only through its web-search MCP server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_deep_research_example.py -> repo root is 3 parents up.
_DEEP_RESEARCH_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "deep-research"


@pytest.fixture(scope="module")
def deep_research_spec() -> AgentSpec:
    """Load and validate the deep-research bundle once for the module."""
    return load(_DEEP_RESEARCH_BUNDLE)


def test_deep_research_loads_with_keenable_mcp(deep_research_spec: AgentSpec) -> None:
    """
    The bundle loads and its only configured web-research source is the
    auto-discovered Keenable MCP server (HTTP transport at the public endpoint).

    There is no ``tools:`` block — the whole point is that a
    ``tools/mcp/<name>.yaml`` server is discovered and its tools exposed. If
    discovery regresses, the agent loads with nothing to search/fetch with.
    """
    assert deep_research_spec.name == "deep-research"
    assert deep_research_spec.sub_agents == []

    servers = {s.name: s for s in deep_research_spec.mcp_servers}
    assert list(servers) == ["keenable"]
    keenable = servers["keenable"]
    assert keenable.transport == "http"
    assert keenable.url == "https://api.keenable.ai/mcp"


def test_deep_research_skill_present(deep_research_spec: AgentSpec) -> None:
    """The deep-research procedure skill is discovered from skills/<name>/SKILL.md."""
    assert sorted(s.name for s in deep_research_spec.skills) == ["deep-research"]


def test_deep_research_runs_on_claude_sdk(deep_research_spec: AgentSpec) -> None:
    """
    The brain is the claude-sdk harness and no model is pinned, so it runs on
    whatever Claude provider the user configured (a pinned Databricks-only id
    would 404 on a plain Anthropic key).
    """
    assert deep_research_spec.executor.config.get("harness") == "claude-sdk"
    assert deep_research_spec.executor.model is None


def test_deep_research_has_no_os_environment(deep_research_spec: AgentSpec) -> None:
    """The example does not opt into local file or shell tools."""
    assert deep_research_spec.os_env is None
