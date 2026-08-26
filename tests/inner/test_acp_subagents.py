"""Tests for the ACP sub-agent dialect seam (:mod:`omnigent.inner.acp_subagents`).

The frame shapes below are copied verbatim from a live ``devin acp`` turn that
delegated three parallel sub-agents (captured 2026-08-25): the lifecycle rides
in vendor ``_meta`` on a ``tool_call_update`` whose ``toolCallId`` is the
sub-agent's ``agentId``. Keeping the real shapes here means the source is tested
against what Devin actually emits, not a paraphrase.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.inner.acp_subagents import (
    DevinSubAgentSource,
    SubAgentEnd,
    SubAgentEvent,
    SubAgentStart,
    read_subagent_events,
)

# --- real captured frames (params.update objects) -----------------------------

_DEVIN_STARTED = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "a0ac9364",
    "status": "in_progress",
    "_meta": {
        "cognition.ai/subagent_started": {
            "agentId": "a0ac9364",
            "title": "mathutils",
            "task": "In the directory /tmp/x create mathutils.py with add/sub/mul and tests.",
        }
    },
}

_DEVIN_COMPLETED = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "a0ac9364",
    "status": "completed",
    "_meta": {
        "cognition.ai/subagent_completed": {
            "agentId": "a0ac9364",
            "success": True,
            "summary": "Created mathutils.py and test_mathutils.py; 3 tests pass.",
        }
    },
}

# A nested tool call the sub-agent made — marks provenance, NOT a lifecycle edge.
_DEVIN_CONTEXT = {
    "sessionUpdate": "tool_call",
    "toolCallId": "toolu_bdrk_01Ha8UacTecWfxbxgERXdGtN",
    "title": "Wrote mathutils.py",
    "_meta": {"cognition.ai/subagent_context": {"parentAgentId": "a0ac9364"}},
}


def test_devin_source_reads_a_start() -> None:
    """``cognition.ai/subagent_started`` → a ``SubAgentStart`` keyed by agentId.

    **What breaks if this fails**: Devin's spawned sub-agent never becomes a
    child session, so the web "Subagents" panel stays empty for it.
    """
    events = DevinSubAgentSource().read(_DEVIN_STARTED)
    assert events == (
        SubAgentStart(
            child_key="a0ac9364",
            title="mathutils",
            task="In the directory /tmp/x create mathutils.py with add/sub/mul and tests.",
        ),
    )


def test_devin_source_reads_a_completion() -> None:
    """``cognition.ai/subagent_completed`` → a ``SubAgentEnd`` with the summary."""
    events = DevinSubAgentSource().read(_DEVIN_COMPLETED)
    assert events == (
        SubAgentEnd(
            child_key="a0ac9364",
            ok=True,
            summary="Created mathutils.py and test_mathutils.py; 3 tests pass.",
        ),
    )


def test_devin_source_ignores_context_marker() -> None:
    """``subagent_context`` marks a nested call's provenance — not a lifecycle edge.

    Treating it as a start/end would mint or close a phantom child on every tool
    call the sub-agent makes.
    """
    assert DevinSubAgentSource().read(_DEVIN_CONTEXT) == ()


def test_devin_source_self_gates_on_plain_frames() -> None:
    """A frame without the Devin markers yields nothing — the source is inert.

    This is the "applies only to acp devin" property: the source recognizes its
    own dialect, so it is a no-op for every other ACP agent's traffic.
    """
    source = DevinSubAgentSource()
    assert source.read({"sessionUpdate": "tool_call", "toolCallId": "x"}) == ()
    assert source.read({"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}) == ()
    assert source.read({"_meta": {"cognition.ai/icon": "wrench"}}) == ()  # unrelated meta
    assert source.read({"_meta": "not-a-dict"}) == ()  # malformed


def test_devin_source_skips_a_blank_agent_id() -> None:
    """A start/complete missing its agentId is dropped, not surfaced with a blank key.

    The agentId is both the correlation and idempotency key, so a blank one would
    mint an unaddressable child.
    """
    source = DevinSubAgentSource()
    assert source.read({"_meta": {"cognition.ai/subagent_started": {"title": "x"}}}) == ()
    assert source.read({"_meta": {"cognition.ai/subagent_started": {"agentId": ""}}}) == ()


def test_devin_source_falls_back_title_to_agent_id() -> None:
    """A start with no title still yields a usable row label (the agentId)."""
    (start,) = DevinSubAgentSource().read(
        {"_meta": {"cognition.ai/subagent_started": {"agentId": "abc123"}}}
    )
    assert isinstance(start, SubAgentStart)
    assert start.child_key == "abc123" and start.title == "abc123" and start.task == ""


def test_read_subagent_events_uses_the_default_devin_source() -> None:
    """The module-level helper wires the default sources (Devin today)."""
    assert read_subagent_events(_DEVIN_STARTED) == [
        SubAgentStart(
            child_key="a0ac9364",
            title="mathutils",
            task="In the directory /tmp/x create mathutils.py with add/sub/mul and tests.",
        )
    ]
    assert read_subagent_events({"sessionUpdate": "tool_call"}) == []


def test_read_subagent_events_accepts_a_new_dialect_source() -> None:
    """A future harness plugs in by passing its own source — no core change.

    Proves the seam: a source keyed on a different marker is honored, and it
    composes with others. This is the extension point for another vendor or the
    eventual ACP-standard subagent convention.
    """

    class _FakeAcpSubAgentSource:
        """A hypothetical agent that marks spawns with its own vendor key."""

        def read(self, update: Mapping[str, Any]) -> Sequence[SubAgentEvent]:
            spawn = update.get("acme.dev/spawn")
            if isinstance(spawn, Mapping) and isinstance(spawn.get("id"), str):
                return (SubAgentStart(child_key=spawn["id"], title=spawn.get("name", "worker")),)
            return ()

    update = {"acme.dev/spawn": {"id": "w1", "name": "indexer"}}
    events = read_subagent_events(update, sources=[_FakeAcpSubAgentSource()])
    assert events == [SubAgentStart(child_key="w1", title="indexer", task="")]
    # And the Devin default is inert on this other dialect's frame.
    assert read_subagent_events(update) == []
