"""Tests for the generic sub-agent seam (:mod:`omnigent.inner.acp_subagents`).

Vendor-free by design: this module must name no agent, so the fixtures here use
invented dialects. Devin's real-frame coverage lives in
``tests/inner/devin/test_devin_subagents.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.inner.acp_subagents import (
    AcpSubAgentSource,
    SubAgentEnd,
    SubAgentEvent,
    SubAgentStart,
    read_subagent_events,
)


class _SpawnDialect:
    """A hypothetical agent that marks spawns with its own vendor key."""

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]:
        """Return a start for ``acme.dev/spawn``, nothing otherwise."""
        spawn = update.get("acme.dev/spawn")
        if isinstance(spawn, Mapping) and isinstance(spawn.get("id"), str):
            return (SubAgentStart(child_key=spawn["id"], title=spawn.get("name", "worker")),)
        return ()


class _DoneDialect:
    """A second, unrelated dialect — proves sources compose."""

    def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]:
        """Return an end for ``acme.dev/done``, nothing otherwise."""
        done = update.get("acme.dev/done")
        if isinstance(done, Mapping) and isinstance(done.get("id"), str):
            return (SubAgentEnd(child_key=done["id"], ok=bool(done.get("ok", True))),)
        return ()


def test_sources_satisfy_the_protocol() -> None:
    """A plain ``read``-shaped object is an :class:`AcpSubAgentSource`.

    The protocol is runtime-checkable so a vendor package can implement it
    without importing a base class — the low bar that keeps a dialect liftable.
    """
    assert isinstance(_SpawnDialect(), AcpSubAgentSource)


def test_read_subagent_events_without_sources_is_inert() -> None:
    """No sources -> no events, whatever the frame carries.

    This is the generic ACP harness's configuration: an agent Omnigent knows
    nothing vendor-specific about must produce no sub-agent surfacing at all.
    """
    assert read_subagent_events({"acme.dev/spawn": {"id": "w1"}}, ()) == []


def test_read_subagent_events_runs_every_source() -> None:
    """Each source contributes; a frame one recognizes is ignored by the other."""
    sources: list[AcpSubAgentSource] = [_SpawnDialect(), _DoneDialect()]

    assert read_subagent_events({"acme.dev/spawn": {"id": "w1", "name": "indexer"}}, sources) == [
        SubAgentStart(child_key="w1", title="indexer", task="")
    ]
    assert read_subagent_events({"acme.dev/done": {"id": "w1", "ok": False}}, sources) == [
        SubAgentEnd(child_key="w1", ok=False, summary="")
    ]
    # Both edges in one frame, in source order.
    both = read_subagent_events(
        {"acme.dev/spawn": {"id": "w2"}, "acme.dev/done": {"id": "w2"}}, sources
    )
    assert both == [SubAgentStart(child_key="w2", title="worker"), SubAgentEnd(child_key="w2")]


def test_unrecognized_frames_yield_nothing() -> None:
    """An ordinary ACP frame passes through every source untouched."""
    sources: list[AcpSubAgentSource] = [_SpawnDialect(), _DoneDialect()]
    for frame in (
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Ran ls"},
        {"_meta": {"some.vendor/unrelated": {"id": "x"}}},
    ):
        assert read_subagent_events(frame, sources) == []
