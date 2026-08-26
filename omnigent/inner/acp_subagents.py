"""Surface an ACP agent's sub-agents as Omnigent child sessions.

The Agent Client Protocol has not standardized sub-agents. An RFD proposes a
``tool_call`` with ``kind: "subagent"`` plus a ``childSessionId`` and a distinct
child ``sessionId``, but no shipping agent emits that yet, so an agent that
spawns sub-agents reports them in its own dialect. Devin, for instance, carries
the whole lifecycle in vendor ``_meta`` on a ``tool_call_update`` whose
``toolCallId`` is the sub-agent's ``agentId``::

    _meta["cognition.ai/subagent_started"]   = {agentId, title, task}
    _meta["cognition.ai/subagent_completed"] = {agentId, success, summary}

An :class:`AcpSubAgentSource` maps one such dialect onto a small normalized
lifecycle (:class:`SubAgentStart` / :class:`SubAgentEnd`).
:class:`~omnigent.inner.acp_executor.AcpExecutor` runs the registered sources
over every ``session/update`` and emits the matching
:class:`~omnigent.inner.executor.SubAgentStarted` /
:class:`~omnigent.inner.executor.SubAgentCompleted` events; the runner turns
those into child sessions via the same ``external_subagent_start`` path the
native harnesses already use, so the web "Subagents" panel lists one row per
child.

Supporting another agent's sub-agents means adding one source here — the
executor, adapter, runner, and server child-session machinery are untouched. A
source **self-gates** by recognizing its own dialect's markers, so it is inert
for agents that don't speak it (Devin's source fires only on ``cognition.ai/*``
keys) and there is no per-harness switch to maintain. When ACP standardizes the
sub-agent convention, a single source keyed on the standard fields covers every
compliant agent at once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SubAgentStart:
    """A sub-agent began. Normalized output of an :class:`AcpSubAgentSource`.

    :param child_key: Stable id for the sub-agent, unique within the parent
        turn. Correlates a later :class:`SubAgentEnd` and is the idempotency key
        when the child session is minted.
    :param title: Short human label for the row, e.g. ``"mathutils"``.
    :param task: The instruction the sub-agent was given, shown on the row.
    """

    child_key: str
    title: str
    task: str = ""


@dataclass(frozen=True)
class SubAgentEnd:
    """A previously-started sub-agent finished.

    :param child_key: Matches the originating :attr:`SubAgentStart.child_key`.
    :param ok: Whether the sub-agent reported success.
    :param summary: The sub-agent's closing summary, shown on the child row.
    """

    child_key: str
    ok: bool = True
    summary: str = ""


SubAgentEvent = SubAgentStart | SubAgentEnd


@runtime_checkable
class AcpSubAgentSource(Protocol):
    """Maps one agent's sub-agent dialect onto the normalized lifecycle.

    Pure and stateless: given a single ACP ``session/update`` payload (the
    ``params.update`` object, which carries ``sessionUpdate`` and any ``_meta``),
    return the sub-agent lifecycle events it carries — almost always none. An
    implementation MUST self-gate on its own dialect's markers so it stays inert
    for agents that don't speak it.
    """

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]: ...


# --- Devin (Cognition) --------------------------------------------------------
# Devin conveys the sub-agent lifecycle only through vendor ``_meta``; there is
# no ACP-standard field to read. The sub-agent's ``agentId`` is the stable key.
_DEVIN_STARTED = "cognition.ai/subagent_started"
_DEVIN_COMPLETED = "cognition.ai/subagent_completed"


class DevinSubAgentSource:
    """Reads Devin's ``cognition.ai/subagent_*`` ``_meta`` lifecycle.

    Fires only when those keys are present, so it is inert for every other ACP
    agent — the ``devin``-only gating is the dialect itself, not a harness check.
    """

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]:
        meta = update.get("_meta")
        if not isinstance(meta, Mapping):
            return ()
        events: list[SubAgentEvent] = []
        started = meta.get(_DEVIN_STARTED)
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
        completed = meta.get(_DEVIN_COMPLETED)
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


# The dialects :class:`AcpExecutor` consults, in order. Each self-gates, so
# listing a source is harmless for agents that don't speak its dialect. Add a
# new vendor source — or the eventual ACP-standard subagent source — here.
DEFAULT_ACP_SUBAGENT_SOURCES: tuple[AcpSubAgentSource, ...] = (DevinSubAgentSource(),)


def read_subagent_events(
    update: Mapping[str, Any],
    sources: Sequence[AcpSubAgentSource] = DEFAULT_ACP_SUBAGENT_SOURCES,
) -> list[SubAgentEvent]:
    """Run every source over one ``session/update``; return all events found.

    :param update: The ACP ``params.update`` object.
    :param sources: Dialects to try; defaults to :data:`DEFAULT_ACP_SUBAGENT_SOURCES`.
    :returns: All sub-agent lifecycle events the sources recognized (usually empty).
    """
    out: list[SubAgentEvent] = []
    for source in sources:
        out.extend(source.read(update))
    return out
