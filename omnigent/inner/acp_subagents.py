"""Normalized sub-agent lifecycle for ACP agents.

The Agent Client Protocol has not standardized sub-agents. An RFD proposes a
``tool_call`` with ``kind: "subagent"`` plus a ``childSessionId`` and a distinct
child ``sessionId``, but no shipping agent emits that yet, so an agent that
spawns sub-agents reports them in its own dialect.

This module owns only the **generic** half: a small normalized lifecycle
(:class:`SubAgentStart` / :class:`SubAgentEnd`) and the
:class:`AcpSubAgentSource` protocol that maps one dialect onto it. It reads no
vendor field and names no vendor;
:class:`~omnigent.inner.acp_executor.AcpExecutor` runs whatever sources its
:class:`~omnigent.inner.acp_extension.AcpExtension` supplies and emits
:class:`~omnigent.inner.executor.SubAgentStarted` /
:class:`~omnigent.inner.executor.SubAgentCompleted`, which the runner turns into
Omnigent child sessions so the web "Subagents" panel lists one row per child.

A vendor's dialect lives with that vendor — see
:class:`omnigent.inner.devin.subagents.DevinSubAgentSource` for the worked
example — so supporting another agent means adding one source in that agent's
own package, with nothing here or downstream to change. When ACP standardizes
the convention, a single source keyed on the standard fields covers every
compliant agent at once, and can ship here rather than per-vendor.
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
    :param summary: The sub-agent's closing summary, shown in the child chat.
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
    return the sub-agent lifecycle events it carries — almost always none.

    An implementation MUST self-gate on its own dialect's markers rather than
    assume it is only handed its own vendor's traffic. Two reasons: a source is
    the only thing that knows its dialect, and self-gating means a source stays
    correct if an agent's fork or a future multi-dialect wrap sends it frames it
    does not recognize.
    """

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]: ...


def read_subagent_events(
    update: Mapping[str, Any],
    sources: Sequence[AcpSubAgentSource],
) -> list[SubAgentEvent]:
    """Run every source over one ``session/update``; return all events found.

    :param update: The ACP ``params.update`` object.
    :param sources: Dialects to try, from the executor's
        :class:`~omnigent.inner.acp_extension.AcpExtension`. Empty (the generic
        ACP harness) short-circuits to no events.
    :returns: All sub-agent lifecycle events the sources recognized (usually
        empty).
    """
    out: list[SubAgentEvent] = []
    for source in sources:
        out.extend(source.read(update))
    return out
