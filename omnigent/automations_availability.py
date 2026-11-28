"""Hide tool groups from callers whose server gate turns them off."""

from __future__ import annotations

from dataclasses import replace

from omnigent.spec.types import AgentSpec

# Sent on the runner's bundle download because the gate is per caller while the
# runner caches one parsed spec per bundle.
AUTOMATIONS_HEADER = "X-Omnigent-Automations"
# Same, for the owner-Inbox tools; runners keep them only when it is "1".
USER_INBOX_HEADER = "X-Omnigent-User-Inbox"
# Same, for the gadget tools; runners keep them only when it is "1".
GADGETS_HEADER = "X-Omnigent-Gadgets"
# Sent by the runner on its bundle download: the tool groups added after older runners
# that it accepts in a spec's ``tool_groups``. The server takes the others out of the
# bundle, because an older runner's parser refuses a group it doesn't know.
ACCEPTED_TOOL_GROUPS_HEADER = "X-Omnigent-Accepted-Tool-Groups"
# Tool groups a runner may not know; ones it doesn't list in that header are taken out.
NEGOTIATED_TOOL_GROUPS: frozenset[str] = frozenset({"gadgets"})


def without_scheduled_task_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the scheduled-task tool group off."""
    return replace(
        spec,
        tool_groups=replace(spec.tool_groups, scheduled_tasks=False),
        sub_agents=[without_scheduled_task_tools(child) for child in spec.sub_agents],
    )


def without_user_inbox_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the user Inbox tool group off."""
    return replace(
        spec,
        tool_groups=replace(spec.tool_groups, user_inbox=False),
        sub_agents=[without_user_inbox_tools(child) for child in spec.sub_agents],
    )


def without_gadget_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the gadget tool group off."""
    return replace(
        spec,
        tool_groups=replace(spec.tool_groups, gadgets=False),
        sub_agents=[without_gadget_tools(child) for child in spec.sub_agents],
    )
