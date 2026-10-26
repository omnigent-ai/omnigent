"""Compaction exports preserve retained context and reject incomplete boundaries."""

import pytest

from omnigent.inner.claude_compaction import ClaudeCompactionTranscript


@pytest.mark.parametrize("preservation", ["messages", "segment", "none"])
@pytest.mark.parametrize("anchor", ["summary", "boundary"])
def test_compaction_retains_messages_in_context_order(preservation: str, anchor: str) -> None:
    transcript = ClaudeCompactionTranscript()
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
    }
    for uuid, content in [("old", "discarded"), ("retained", [image])]:
        transcript.observe({"type": "user", "uuid": uuid, "message": {"content": content}})
    metadata = {"trigger": "manual"}
    if preservation == "messages":
        metadata["preserved_messages"] = {"anchor_uuid": anchor, "uuids": ["retained"]}
    elif preservation == "segment":
        metadata["preserved_segment"] = {
            "anchor_uuid": anchor,
            "head_uuid": "retained",
            "tail_uuid": "retained",
        }
    transcript.observe(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "compact_metadata": metadata,
        }
    )
    assert transcript.checkpoint_since(0) is None
    transcript.observe(
        {"type": "user", "uuid": "summary", "isSynthetic": True, "message": {"content": "summary"}}
    )
    transcript.observe({"type": "assistant", "uuid": "next", "message": {"content": "continued"}})
    expected = ["summary"]
    if preservation != "none":
        expected.insert(0 if anchor == "boundary" else 1, [image])
    expected.append("continued")
    checkpoint = transcript.checkpoint_since(0)
    assert checkpoint is not None
    assert [entry["content"] for entry in checkpoint] == expected
    assert transcript.checkpoint_since(transcript.generation) is None


@pytest.mark.parametrize(
    "incomplete",
    ["status", "missing_summary", "empty_summary", "missing_retained", "bad_metadata"],
)
def test_incomplete_compaction_never_exports_old_or_empty_context(incomplete: str) -> None:
    transcript = ClaudeCompactionTranscript()
    transcript.observe({"type": "user", "uuid": "old", "message": {"content": "old history"}})
    if incomplete == "status":
        transcript.observe({"type": "system", "subtype": "status", "status": "compacting"})
    else:
        metadata = {}
        if incomplete == "missing_retained":
            metadata["preserved_messages"] = {"anchor_uuid": "summary", "uuids": ["not-observed"]}
        elif incomplete == "bad_metadata":
            metadata["preserved_messages"] = "unknown-format"
        transcript.observe(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "compact_metadata": metadata,
            }
        )
        if incomplete != "missing_summary":
            transcript.observe(
                {
                    "type": "user",
                    "uuid": "summary",
                    "isSynthetic": True,
                    "message": {"content": "" if incomplete == "empty_summary" else "summary"},
                }
            )
    assert transcript.checkpoint_since(0) is None


def test_multiple_compactions_only_export_the_latest_complete_boundary() -> None:
    transcript = ClaudeCompactionTranscript()
    for number in range(2):
        transcript.observe(
            {"type": "system", "subtype": "compact_boundary", "uuid": f"boundary-{number}"}
        )
        transcript.observe(
            {
                "type": "user",
                "uuid": f"summary-{number}",
                "isSynthetic": True,
                "message": {"content": f"summary {number}"},
            }
        )
    assert transcript.checkpoint_since(0) == [
        {"type": "message", "role": "user", "content": "summary 1"}
    ]
    transcript.observe({"type": "system", "subtype": "compact_boundary", "uuid": "incomplete"})
    assert transcript.checkpoint_since(0) is None


def test_subagent_compaction_does_not_replace_parent_context() -> None:
    transcript = ClaudeCompactionTranscript()
    transcript.observe(
        {"type": "system", "subtype": "compact_boundary", "parent_tool_use_id": "agent-call"}
    )
    transcript.observe(
        {
            "type": "user",
            "uuid": "summary",
            "isSynthetic": True,
            "parent_tool_use_id": "agent-call",
            "message": {"content": "subagent summary"},
        }
    )
    assert transcript.generation == 0
    assert transcript.checkpoint_since(0) is None


def test_custom_transport_preserves_permission_routing_and_live_stderr_callback() -> None:
    from claude_agent_sdk import ClaudeAgentOptions

    from omnigent.inner.claude_compaction import compaction_transport

    async def gate(*args):
        raise AssertionError("transport construction must not invoke the permission gate")

    options = ClaudeAgentOptions(can_use_tool=gate)
    transcript = ClaudeCompactionTranscript()
    transport = compaction_transport(options, transcript)
    assert transport._options.permission_prompt_tool_name == "stdio"
    assert options.permission_prompt_tool_name is None
    lines = []
    options.stderr = lines.append
    transport._options.stderr("connected")
    options.stderr = None
    transport._options.stderr("closed")
    assert lines == ["connected"]
