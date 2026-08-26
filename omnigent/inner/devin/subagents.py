"""Devin's sub-agent dialect.

Devin (Cognition's ``devin`` CLI, driven through ``devin acp``) delegates work to
parallel sub-agents but reports the lifecycle only in vendor ``_meta`` — it emits
none of the ACP sub-agent RFD's fields. Captured from a live turn that spawned
three sub-agents (2026-08-25): no ``kind: "subagent"``, no ``childSessionId``,
one session, and the whole lifecycle on a ``tool_call_update`` whose
``toolCallId`` is the sub-agent's ``agentId``::

    _meta["cognition.ai/subagent_started"]   = {agentId, title, task}
    _meta["cognition.ai/subagent_completed"] = {agentId, success, summary}
    _meta["cognition.ai/subagent_context"]   = {parentAgentId}   # provenance only

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

from omnigent.inner.acp_subagents import SubAgentEnd, SubAgentEvent, SubAgentStart

# Devin conveys the sub-agent lifecycle only through these vendor ``_meta`` keys;
# the sub-agent's ``agentId`` is the stable key across both edges.
_STARTED = "cognition.ai/subagent_started"
_COMPLETED = "cognition.ai/subagent_completed"


class DevinSubAgentSource:
    """Reads Devin's ``cognition.ai/subagent_*`` ``_meta`` lifecycle.

    Fires only when those keys are present, so it stays inert for any frame that
    does not carry Devin's dialect — the self-gating the
    :class:`~omnigent.inner.acp_subagents.AcpSubAgentSource` protocol requires.
    """

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]:
        """Return the sub-agent lifecycle events carried by one ``session/update``.

        :param update: The ACP ``params.update`` object.
        :returns: Normalized start/end events, empty for a non-Devin frame.
        """
        meta = update.get("_meta")
        if not isinstance(meta, Mapping):
            return ()
        events: list[SubAgentEvent] = []
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
