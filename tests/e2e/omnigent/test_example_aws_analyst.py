"""Structural test for the AWS Analyst example (examples/aws_analyst).

AWS Analyst is a single-agent recipe that answers questions over governed AWS
data through two official AWS Labs MCP servers — Amazon Redshift
(``awslabs.redshift-mcp-server``) and Amazon S3 Tables
(``awslabs.s3-tables-mcp-server``) — wired as inline ``type: mcp`` stdio tools
launched via ``uvx``. Both are read-only by default. Pure spec-load — no LLM, no
credentials, no live AWS account (``expand_env=False`` so the ``${AWS_PROFILE}``
/ ``${AWS_REGION}`` refs don't need to resolve).

What breaks if this fails:
- an MCP connector is dropped or renamed (the agent loses Redshift or S3 Tables),
- a connector stops launching via ``uvx`` / the ``awslabs.*`` package id drifts
  (the recipe's "no custom connector code" promise regresses),
- the Redshift tool allow-list is dropped or loses ``execute_query`` (the
  governed-analytics surface stops being a curated allow-list),
- the S3 Tables server gains ``--allow-write`` (the read-only guarantee the
  README makes is broken),
- the agent silently pins a model (re-coupling it to one provider — a
  Databricks-only id would 404 on a plain Anthropic / OpenAI key),
- a sub-agent appears (this is deliberately a single agent, not an orchestrator).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_aws_analyst.py -> repo root is 3 parents up.
_AWS_ANALYST_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "aws_analyst"


@pytest.fixture(scope="module")
def aws_analyst_spec() -> AgentSpec:
    """Load and validate the aws_analyst bundle once for the module.

    ``expand_env=False`` so the structural tests run without a live
    ``AWS_PROFILE`` / ``AWS_REGION`` in the environment.
    """
    return load(_AWS_ANALYST_BUNDLE, expand_env=False)


def test_aws_analyst_name_and_harness(aws_analyst_spec: AgentSpec) -> None:
    """
    The agent is named ``aws_analyst`` and runs on the claude-sdk harness with
    no pinned model or profile, so it inherits whatever Claude provider the user
    configured. Re-introducing a pin would re-couple the recipe to one provider.
    """
    assert aws_analyst_spec.name == "aws_analyst"
    assert aws_analyst_spec.executor.config.get("harness") == "claude-sdk"
    assert aws_analyst_spec.executor.model is None
    assert aws_analyst_spec.executor.profile is None


def test_aws_analyst_is_single_agent(aws_analyst_spec: AgentSpec) -> None:
    """AWS Analyst is a single agent — no sub-agents, no delegation."""
    assert aws_analyst_spec.sub_agents == []
    assert aws_analyst_spec.tools.agents == []


def test_aws_analyst_wires_both_awslabs_mcp_servers(aws_analyst_spec: AgentSpec) -> None:
    """
    Both AWS Labs MCP servers are wired as inline stdio connectors launched via
    ``uvx``, and the ``awslabs.*`` package ids are the ones the recipe promises.

    A dropped/renamed connector, a non-stdio transport, or a drifted package id
    all break the "any AWS Labs MCP server plugs in as a ``type: mcp`` tool with
    no custom connector code" promise the README makes.
    """
    by_name = {s.name: s for s in aws_analyst_spec.mcp_servers}
    assert sorted(by_name) == ["redshift", "s3-tables"]

    for server in by_name.values():
        assert server.transport == "stdio", server.name
        assert server.command == "uvx", server.name

    assert by_name["redshift"].args == ["awslabs.redshift-mcp-server@latest"]
    assert by_name["s3-tables"].args == ["awslabs.s3-tables-mcp-server@latest"]


def test_aws_analyst_redshift_tool_allowlist(aws_analyst_spec: AgentSpec) -> None:
    """
    The Redshift connector surfaces a curated allow-list (discovery + read-only
    SQL), not the server's full tool surface. ``execute_query`` must be present
    (the agent can't answer questions without it) alongside the ``list_*``
    discovery tools the prompt tells it to walk first.
    """
    redshift = next(s for s in aws_analyst_spec.mcp_servers if s.name == "redshift")
    assert redshift.tools == [
        "list_clusters",
        "list_databases",
        "list_schemas",
        "list_tables",
        "list_columns",
        "execute_query",
    ]


def test_aws_analyst_is_read_only(aws_analyst_spec: AgentSpec) -> None:
    """
    Neither connector is granted write access. The S3 Tables server defaults to
    read-only; the recipe must never pass ``--allow-write`` (the README's
    read-only guarantee). The Redshift allow-list also exposes no mutating tool.
    """
    by_name = {s.name: s for s in aws_analyst_spec.mcp_servers}
    for server in by_name.values():
        assert "--allow-write" not in server.args, server.name

    # The Redshift allow-list is read + discovery only — no write verbs leak in.
    redshift_tools = by_name["redshift"].tools or []
    for tool in redshift_tools:
        assert not any(
            verb in tool for verb in ("insert", "update", "delete", "write", "create", "drop")
        ), tool
