"""Hide the scheduled-task tools from callers the automations gate turns off."""

from __future__ import annotations

from dataclasses import replace

from omnigent.spec.types import AgentSpec

# Sent on the runner's bundle download because the gate is per caller while the
# runner caches one parsed spec per bundle.
AUTOMATIONS_HEADER = "X-Omnigent-Automations"


def without_scheduled_task_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the scheduled-task tool group off."""
    return replace(
        spec,
        tool_groups=replace(spec.tool_groups, scheduled_tasks=False),
        sub_agents=[without_scheduled_task_tools(child) for child in spec.sub_agents],
    )
