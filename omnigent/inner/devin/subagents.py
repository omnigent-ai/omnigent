"""Devin's sub-agent dialect.

Devin (Cognition's ``devin`` CLI, driven through ``devin acp``) delegates work to
parallel sub-agents but reports the lifecycle only in vendor ``_meta`` — it emits
none of the ACP sub-agent RFD's fields. Captured from a live turn that spawned
three sub-agents (2026-08-25): no ``kind: "subagent"``, no ``childSessionId``,
one session, and the whole lifecycle on a ``tool_call_update`` whose
``toolCallId`` is the sub-agent's ``agentId``::

    _meta["cognition.ai/subagent_started"]   = {agentId, title, task}       # tool_call_update
    _meta["cognition.ai/subagent_completed"] = {agentId, success, summary}  # tool_call_update
    _meta["cognition.ai/subagent_context"]   = {parentAgentId}              # on the sub-agent's
                                                                            # own tool_call frames

Reading vendor ``_meta`` is not a shortcut here — it is the only structured
sub-agent signal Devin emits, so there is no generic field to prefer. Confining
it to this module is what keeps that coupling from reaching the generic ACP
executor.

Devin needs no capability negotiation: the same capture shows it emitting these
keys while Omnigent advertised only ``clientCapabilities.fs``. That is dialect-
specific, not a protocol rule — Claude Code's ACP bridge withholds its nested
transcript unless the client opts in via
``clientCapabilities._meta["subagent-transcript"]``, and keys parentage on
``_meta.claudeCode.parentToolUseId`` instead. Two vendors, one concept, no shared
field: the reason a dialect is per-vendor rather than a branch in the executor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.inner.acp_subagents import (
    SubAgentActivity,
    SubAgentEnd,
    SubAgentEvent,
    SubAgentStart,
)

# Devin conveys the sub-agent lifecycle only through these vendor ``_meta`` keys;
# the sub-agent's ``agentId`` is the stable key across both edges. ``context``
# tags the sub-agent's *own* tool calls with the owning agent, so they can be
# routed into that sub-agent's child transcript instead of the parent stream.
_STARTED = "cognition.ai/subagent_started"
_COMPLETED = "cognition.ai/subagent_completed"
_CONTEXT = "cognition.ai/subagent_context"


class DevinSubAgentSource:
    """Reads Devin's ``cognition.ai/subagent_*`` ``_meta`` lifecycle.

    Fires only when those keys are present, so it stays inert for any frame that
    does not carry Devin's dialect — the self-gating the
    :class:`~omnigent.inner.acp_subagents.AcpSubAgentSource` protocol requires.
    """

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]:
        """Return the sub-agent events carried by one ``session/update``.

        :param update: The ACP ``params.update`` object.
        :returns: Normalized start / end / activity events, empty for a non-Devin
            frame. At most one applies per frame (the lifecycle keys ride
            ``tool_call_update``; ``subagent_context`` rides the sub-agent's own
            ``tool_call``).
        """
        meta = update.get("_meta")
        if not isinstance(meta, Mapping):
            return ()
        events: list[SubAgentEvent] = []

        # The sub-agent's own tool call — route it to that sub-agent's child.
        context = meta.get(_CONTEXT)
        if isinstance(context, Mapping) and update.get("sessionUpdate") == "tool_call":
            parent_agent_id = context.get("parentAgentId")
            call_id = update.get("toolCallId")
            if (
                isinstance(parent_agent_id, str)
                and parent_agent_id
                and isinstance(call_id, str)
                and call_id
            ):
                raw_input = update.get("rawInput")
                events.append(
                    SubAgentActivity(
                        child_key=parent_agent_id,
                        call_id=call_id,
                        name=str(update.get("title") or update.get("kind") or "tool"),
                        args=raw_input if isinstance(raw_input, Mapping) else {},
                    )
                )

        started = meta.get(_STARTED)
        if isinstance(started, Mapping):
            agent_id = started.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                events.append(
                    SubAgentStart(
                        child_key=agent_id,
                        title=str(started.get("title") or agent_id),
                        task=str(started.get("task") or ""),
                    )
                )
        completed = meta.get(_COMPLETED)
        if isinstance(completed, Mapping):
            agent_id = completed.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                events.append(
                    SubAgentEnd(
                        child_key=agent_id,
                        ok=bool(completed.get("success", True)),
                        summary=str(completed.get("summary") or ""),
                    )
                )
        return tuple(events)
