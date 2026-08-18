"""Structural test for the Parallel Search example bundle.

The bundle uses Omnigent's standard auto-discovered HTTP MCP declaration. This
test loads the spec without credentials or a live connection to the public
endpoint, so it protects the example's wiring while keeping CI deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_parallel_search.py -> repo root is 3 parents up.
_PARALLEL_SEARCH_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "parallel_search"


@pytest.fixture(scope="module")
def parallel_search_spec() -> AgentSpec:
    """Load and validate the Parallel Search bundle once for the module."""
    return load(_PARALLEL_SEARCH_BUNDLE)


def test_parallel_search_loads_with_parallel_mcp(parallel_search_spec: AgentSpec) -> None:
    """The bundle discovers exactly one configured Parallel HTTP MCP server."""
    assert parallel_search_spec.name == "parallel-search"
    assert parallel_search_spec.sub_agents == []

    servers = {server.name: server for server in parallel_search_spec.mcp_servers}
    assert list(servers) == ["parallel"]
    parallel = servers["parallel"]
    assert parallel.transport == "http"
    assert parallel.url == "https://search.parallel.ai/mcp"


def test_parallel_search_skill_present(parallel_search_spec: AgentSpec) -> None:
    """The web-research procedure is discovered from skills/."""
    assert sorted(skill.name for skill in parallel_search_spec.skills) == ["parallel-search"]


def test_parallel_search_runs_on_claude_sdk(parallel_search_spec: AgentSpec) -> None:
    """The example leaves the LLM model to the configured Claude provider."""
    assert parallel_search_spec.executor.config.get("harness") == "claude-sdk"
    assert parallel_search_spec.executor.model is None


def test_parallel_search_has_no_os_environment(parallel_search_spec: AgentSpec) -> None:
    """The example exposes only its remote web-research MCP tools."""
    assert parallel_search_spec.os_env is None
