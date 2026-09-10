"""Tests for Devin's sub-agent dialect (:mod:`omnigent.inner.devin.subagents`).

The frame shapes below are copied verbatim from a live ``devin acp`` turn that
delegated three parallel sub-agents (captured 2026-08-25): the lifecycle rides
in vendor ``_meta`` on a ``tool_call_update`` whose ``toolCallId`` is the
sub-agent's ``agentId``. Keeping the real shapes here means the source is tested
against what Devin actually emits, not a paraphrase.
"""

from __future__ import annotations

from omnigent.inner.acp_subagents import SubAgentActivity, SubAgentEnd, SubAgentStart
from omnigent.inner.devin import DEVIN_ACP_EXTENSION, DevinSubAgentSource

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

# A nested tool call the sub-agent made — tagged with the owning agentId so it
# can be routed into that sub-agent's child transcript.
_DEVIN_CONTEXT = {
    "sessionUpdate": "tool_call",
    "toolCallId": "toolu_bdrk_01Ha8UacTecWfxbxgERXdGtN",
    "title": "Wrote /tmp/x/mathutils.py",
    "kind": "edit",
    "rawInput": {"file_path": "/tmp/x/mathutils.py", "content": "def add(a, b): ..."},
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


def test_devin_source_reads_context_as_activity() -> None:
    """``subagent_context`` on a ``tool_call`` → a ``SubAgentActivity`` for the child.

    **What breaks if this fails**: the sub-agent's own work (its file writes,
    commands) never reaches its child transcript — the child chat shows only the
    task and summary. ``parentAgentId`` is the child_key, so the runner can route
    the card to the right sub-agent.
    """
    events = DevinSubAgentSource().read(_DEVIN_CONTEXT)
    assert events == (
        SubAgentActivity(
            child_key="a0ac9364",
            call_id="toolu_bdrk_01Ha8UacTecWfxbxgERXdGtN",
            name="Wrote /tmp/x/mathutils.py",
            args={"file_path": "/tmp/x/mathutils.py", "content": "def add(a, b): ..."},
        ),
    )


def test_devin_source_ignores_context_on_a_non_tool_call() -> None:
    """A ``subagent_context`` marker off a ``tool_call`` frame is not an activity.

    Only the sub-agent's own ``tool_call`` frames are its work; the same marker on
    another update kind is not a tool card, so it is left alone (and not routed).
    """
    assert (
        DevinSubAgentSource().read(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "toolu_x",
                "status": "in_progress",
                "_meta": {"cognition.ai/subagent_context": {"parentAgentId": "a0ac9364"}},
            }
        )
        == ()
    )


def test_devin_source_skips_context_with_a_blank_parent_or_call_id() -> None:
    """Activity needs both a parent agent id and a call id to be addressable."""
    source = DevinSubAgentSource()
    assert (
        source.read(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "c1",
                "_meta": {"cognition.ai/subagent_context": {}},
            }
        )
        == ()
    )
    assert (
        source.read(
            {
                "sessionUpdate": "tool_call",
                "_meta": {"cognition.ai/subagent_context": {"parentAgentId": "a0"}},
            }
        )
        == ()
    )


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


def test_devin_extension_declares_the_dialect() -> None:
    """The extension Devin's wrap injects carries exactly this dialect.

    The composition root: without it the executor scans nothing and the Subagents
    panel stays empty for Devin, however correct the source below is.
    """
    assert DEVIN_ACP_EXTENSION.name == "devin"
    assert [type(src) for src in DEVIN_ACP_EXTENSION.subagent_sources] == [DevinSubAgentSource]
    # The declared ``subagents`` capability is derived from this, so it must agree.
    assert DEVIN_ACP_EXTENSION.surfaces_subagents is True


def test_devin_dialect_reaches_the_executor_through_the_extension() -> None:
    """A Devin frame becomes normalized events when the extension is injected.

    Covers the dialect -> extension -> generic executor path in one assertion, so
    a rename on either side of the seam fails here rather than silently.
    """
    from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
    from omnigent.inner.executor import (
        SubAgentCompleted,
        SubAgentStarted,
        SubAgentToolCall,
        ToolCallComplete,
    )

    ex = AcpExecutor(AcpAgentConfig(command="devin acp"), extension=DEVIN_ACP_EXTENSION)
    started = ex._handle_session_update(_DEVIN_STARTED)
    activity = ex._handle_session_update(_DEVIN_CONTEXT)
    completed = ex._handle_session_update(_DEVIN_COMPLETED)

    assert [e for e in started if isinstance(e, SubAgentStarted)] == [
        SubAgentStarted(
            child_key="a0ac9364",
            title="mathutils",
            task="In the directory /tmp/x create mathutils.py with add/sub/mul and tests.",
        )
    ]
    assert [e for e in activity if isinstance(e, SubAgentToolCall)] == [
        SubAgentToolCall(
            child_key="a0ac9364",
            call_id="toolu_bdrk_01Ha8UacTecWfxbxgERXdGtN",
            name="Wrote /tmp/x/mathutils.py",
            args={"file_path": "/tmp/x/mathutils.py", "content": "def add(a, b): ..."},
        )
    ]
    assert [e for e in completed if isinstance(e, SubAgentCompleted)] == [
        SubAgentCompleted(
            child_key="a0ac9364",
            ok=True,
            summary="Created mathutils.py and test_mathutils.py; 3 tests pass.",
        )
    ]
    # The sub-agent's frames never leak into the parent stream as tool cards —
    # in particular the completed lifecycle frame must not close a spurious
    # "tool" card (its toolCallId was never an originating tool_call).
    assert not any(isinstance(e, ToolCallComplete) for e in completed)
