"""Hide tool groups from callers whose server gate turns them off."""

from __future__ import annotations

from dataclasses import replace

from omnigent.spec.types import AgentSpec

# Sent on the runner's bundle download because the gate is per caller while the
# runner caches one parsed spec per bundle.
AUTOMATIONS_HEADER = "X-Omnigent-Automations"
# Same, for the owner-Inbox tools. Servers before default-on agent Inbox
# items send only this one, and accept posts only from agents whose spec
# sets ``tool_groups.user_inbox: true``.
USER_INBOX_HEADER = "X-Omnigent-User-Inbox"
# Sent with it by servers that accept posts from every agent that does not opt out.
AGENT_INBOX_HEADER = "X-Omnigent-Agent-Inbox"


def without_scheduled_task_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the scheduled-task tool group off."""
    return replace(
        spec,
        tool_groups=replace(spec.tool_groups, scheduled_tasks=False),
        sub_agents=[without_scheduled_task_tools(child) for child in spec.sub_agents],
    )


def with_only_opted_in_user_inbox_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the Inbox tools only where the spec sets them on."""
    return replace(
        spec,
        tool_groups=replace(
            spec.tool_groups,
            user_inbox=spec.tool_groups.user_inbox and "user_inbox" in spec.tool_groups_set,
        ),
        sub_agents=[with_only_opted_in_user_inbox_tools(child) for child in spec.sub_agents],
    )


def without_user_inbox_tools(spec: AgentSpec) -> AgentSpec:
    """Return *spec* and its sub-agents with the user Inbox tool group off."""
    return replace(
        spec,
        tool_groups=replace(spec.tool_groups, user_inbox=False),
        sub_agents=[without_user_inbox_tools(child) for child in spec.sub_agents],
    )
