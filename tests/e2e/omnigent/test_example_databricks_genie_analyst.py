"""Structural test for the Databricks Genie Analyst example
(examples/databricks_genie_analyst).

Databricks Genie Analyst is a single-agent recipe that answers natural-language
questions over Unity Catalog-governed data through a Databricks Genie space,
wired as an inline ``type: mcp`` HTTP connector against the Databricks-managed
MCP server (``/api/2.0/mcp/genie/<space-id>``). Auth is handled by
``databricks_profile`` (omnigent mints an OAuth bearer token at connect time),
so no secret lives in the spec. Pure spec-load — no LLM, no credentials, no live
workspace (``expand_env=False`` so the ``${DATABRICKS_*}`` refs don't need to
resolve).

What breaks if this fails:
- the Genie connector is dropped or renamed (the agent loses its only data source),
- the connector stops being HTTP transport / loses its ``url`` (the managed-MCP
  shape the README promises regresses),
- the managed-MCP URL path drifts away from ``/api/2.0/mcp/genie/`` (the recipe
  no longer targets a Genie space),
- ``databricks_profile`` auth is dropped OR a literal token is baked into the
  spec (the "no secret in the config" guarantee breaks),
- the agent silently pins a model (re-coupling it to one provider — a
  Databricks-only id would 404 on a plain Anthropic / OpenAI key),
- a sub-agent appears (this is deliberately a single agent, not an orchestrator).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_databricks_genie_analyst.py -> repo root is 3 parents up.
_GENIE_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "databricks_genie_analyst"


@pytest.fixture(scope="module")
def genie_spec() -> AgentSpec:
    """Load and validate the databricks_genie_analyst bundle once for the module.

    ``expand_env=False`` so the structural tests run without a live
    ``DATABRICKS_HOSTNAME`` / ``DATABRICKS_GENIE_SPACE_ID`` / ``DATABRICKS_PROFILE``
    in the environment.
    """
    return load(_GENIE_BUNDLE, expand_env=False)


def test_genie_name_and_harness(genie_spec: AgentSpec) -> None:
    """
    The agent is named ``databricks_genie_analyst`` and runs on the claude-sdk
    harness with no pinned model or profile, so it inherits whatever Claude
    provider the user configured. Re-introducing a pin would re-couple the recipe
    to one provider.
    """
    assert genie_spec.name == "databricks_genie_analyst"
    assert genie_spec.executor.config.get("harness") == "claude-sdk"
    assert genie_spec.executor.model is None
    assert genie_spec.executor.profile is None


def test_genie_is_single_agent(genie_spec: AgentSpec) -> None:
    """Genie Analyst is a single agent — no sub-agents, no delegation."""
    assert genie_spec.sub_agents == []
    assert genie_spec.tools.agents == []


def test_genie_wires_managed_mcp_over_http(genie_spec: AgentSpec) -> None:
    """
    The single connector is the Databricks-managed Genie MCP server, wired as an
    HTTP connector whose URL targets the documented managed-MCP Genie path.

    A dropped/renamed connector, a non-http transport, or a URL that no longer
    points at ``/api/2.0/mcp/genie/`` all break the "reach a Genie space over the
    managed MCP server, no custom connector code" promise the README makes.
    """
    by_name = {s.name: s for s in genie_spec.mcp_servers}
    assert sorted(by_name) == ["genie"]

    genie = by_name["genie"]
    assert genie.transport == "http"
    assert genie.url is not None
    assert "/api/2.0/mcp/genie/" in genie.url
    # stdio-only fields must be absent on an HTTP connector.
    assert genie.command is None


def test_genie_auth_is_profile_based_no_baked_secret(genie_spec: AgentSpec) -> None:
    """
    Auth goes through ``databricks_profile`` (omnigent mints an OAuth token at
    connect time), and no literal bearer token is baked into the spec. If someone
    swaps to an explicit ``Authorization`` header it must reference an env var,
    never a hardcoded secret.
    """
    genie = next(s for s in genie_spec.mcp_servers if s.name == "genie")

    # Profile-based auth is the recipe's default.
    assert genie.databricks_profile is not None

    # No literal token anywhere: with expand_env=False any Authorization header
    # value must still be an unexpanded ${...} ref, never a real token.
    auth = (genie.headers or {}).get("Authorization")
    if auth is not None:
        assert "${" in auth, "Authorization header must reference an env var, not a baked token"
        assert "Bearer ${" in auth or "${" in auth


def test_genie_is_read_only(genie_spec: AgentSpec) -> None:
    """
    The Genie MCP surface is read-only (question -> governed SQL -> results). The
    recipe must not carry any write-enabling flag or tool allow-list entry that
    implies mutation.
    """
    genie = next(s for s in genie_spec.mcp_servers if s.name == "genie")
    for tool in genie.tools or []:
        assert not any(
            verb in tool.lower()
            for verb in ("insert", "update", "delete", "write", "create", "drop")
        ), tool
