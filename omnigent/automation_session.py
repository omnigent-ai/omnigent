"""What a session's agent gets because an automation serves the session.

A session serves an automation when an automation created it or has sent it a
run's prompt; the server-owned labels below record both. Such a session gets
the objective tools unless its agent spec decides otherwise
(``tool_groups.objectives``). The server tells the runner along with the
session's agent bundle, and folds the same fact into the session's agent
revision, so a session an automation starts continuing gets the tools on the
automation's first turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from omnigent.mcp_selection import mcp_selection_revision
from omnigent.spec.types import AgentSpec
from omnigent.stores.conversation_store import (
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SCHEDULED_TASK_RUN_LABEL_KEY,
)

# Response header on a session's agent bundle: "1" when an automation serves it.
AUTOMATION_SESSION_HEADER = "X-Omnigent-Automation-Session"


def serves_automation(labels: Mapping[str, str]) -> bool:
    """Whether an automation created the session or has sent it a run's prompt."""
    return bool(
        labels.get(SCHEDULED_TASK_ORIGIN_LABEL_KEY) or labels.get(SCHEDULED_TASK_RUN_LABEL_KEY)
    )


def session_agent_revision(bundle_location: str, labels: dict[str, str]) -> str:
    """The session's agent revision: its bundle, MCP selections and automation use.

    The runner rebuilds a session's spec when this changes, so the objective
    tools appear once an automation starts serving the session.
    """
    revision = mcp_selection_revision(bundle_location, labels)
    return f"{revision}:automation" if serves_automation(labels) else revision


def with_automation_tools(spec: AgentSpec, *, automation: bool) -> AgentSpec:
    """Turn the objective tools on for an automation session the spec leaves undecided."""
    if not automation or spec.tool_groups.objectives is not None:
        return spec
    return replace(spec, tool_groups=replace(spec.tool_groups, objectives=True))
