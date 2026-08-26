"""Tests for Devin's sub-agent dialect (:mod:`omnigent.inner.devin.subagents`).

The frame shapes below are copied verbatim from a live ``devin acp`` turn that
delegated three parallel sub-agents (captured 2026-08-25): the lifecycle rides
in vendor ``_meta`` on a ``tool_call_update`` whose ``toolCallId`` is the
sub-agent's ``agentId``. Keeping the real shapes here means the source is tested
against what Devin actually emits, not a paraphrase.
"""

from __future__ import annotations

from omnigent.inner.acp_subagents import SubAgentEnd, SubAgentStart
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
    from omnigent.inner.executor import SubAgentCompleted, SubAgentStarted

    ex = AcpExecutor(AcpAgentConfig(command="devin acp"), extension=DEVIN_ACP_EXTENSION)
    started = ex._handle_session_update(_DEVIN_STARTED)
    completed = ex._handle_session_update(_DEVIN_COMPLETED)

    assert [e for e in started if isinstance(e, SubAgentStarted)] == [
        SubAgentStarted(
            child_key="a0ac9364",
            title="mathutils",
            task="In the directory /tmp/x create mathutils.py with add/sub/mul and tests.",
        )
    ]
    assert [e for e in completed if isinstance(e, SubAgentCompleted)] == [
        SubAgentCompleted(
            child_key="a0ac9364",
            ok=True,
            summary="Created mathutils.py and test_mathutils.py; 3 tests pass.",
        )
    ]
