"""AgentObject carries git provenance from the Agent entity."""

from __future__ import annotations

from omnigent.entities import Agent
from omnigent.server.routes.builtin_agents import _to_agent_object


def test_agent_object_includes_git_fields():
    agent = Agent(
        id="ag_1",
        created_at=0,
        name="git-agent",
        bundle_location="ag_1/x",
        git_url="https://github.com/org/repo",
        git_ref="main",
        git_commit="abc123",
    )
    obj = _to_agent_object(agent, agent_cache=None)  # cache-None path returns empty spec fields
    assert obj.git_url == "https://github.com/org/repo"
    assert obj.git_ref == "main"
    assert obj.git_commit == "abc123"
    assert obj.git_subpath is None
