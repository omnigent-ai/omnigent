"""Hide the memory tools from runners whose server cannot serve them."""

from __future__ import annotations

from dataclasses import replace

from omnigent.spec.types import AgentSpec

# Sent on the runner's bundle download. Unlike the automations gate, a missing
# header means "no memory": servers that predate it have no /v1/memory routes.
MEMORY_HEADER = "X-Omnigent-Memory"
# "1" when the server's memory backend can search; otherwise memory_search is hidden.
MEMORY_SEARCH_HEADER = "X-Omnigent-Memory-Search"


def without_memory_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the memory tool group off."""
    return replace(
        spec,
        tool_groups=replace(spec.tool_groups, memory=False),
        sub_agents=[without_memory_tools(child) for child in spec.sub_agents],
    )


def without_memory_search(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents without ``memory_search``."""
    return replace(
        spec,
        memory_search_available=False,
        sub_agents=[without_memory_search(child) for child in spec.sub_agents],
    )
