"""Tests for the server-side ``/btw`` transcript excerpt builder."""

from __future__ import annotations

from omnigent.entities import (
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    RoutingDecisionData,
    SideQuestionData,
)
from omnigent.server.side_questions import (
    build_transcript_excerpt,
    render_item_for_excerpt,
)


def _message(item_id: str, role: str, text: str, *, is_meta: bool = False) -> ConversationItem:
    """Build a message item for excerpt tests."""
    key = "input_text" if role == "user" else "output_text"
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="turn_1",
        created_at=1,
        data=MessageData(
            role=role,
            content=[{"type": key, "text": text}],
            is_meta=is_meta,
            **({"agent": "test-agent"} if role == "assistant" else {}),
        ),
    )


# ── render_item_for_excerpt ──────────────────────────────


def test_renders_messages_with_their_role() -> None:
    """A reader (and the model) needs to know who said what."""
    assert render_item_for_excerpt(_message("m1", "user", "fix the parser")) == (
        "user: fix the parser"
    )
    assert render_item_for_excerpt(_message("m2", "assistant", "on it")) == "assistant: on it"


def test_skips_meta_messages() -> None:
    """Hidden skill-instruction blobs are machinery, not conversation."""
    assert (
        render_item_for_excerpt(_message("m3", "user", "<skill>…</skill>", is_meta=True)) is None
    )


def test_skips_empty_messages() -> None:
    """The workflow persists trailing empty assistant items; drop them."""
    assert render_item_for_excerpt(_message("m4", "assistant", "   ")) is None


def test_skips_non_content_items() -> None:
    """
    Metadata items never reach the excerpt.

    Covers the case that matters most: an earlier ``/btw`` must not
    feed the next one, or asides would compound into context the
    feature exists to avoid.
    """
    aside = ConversationItem(
        id="sq1",
        type="side_question",
        status="completed",
        response_id="turn_btw",
        created_at=1,
        data=SideQuestionData(agent="claude-sdk", question="q", answer="a"),
    )
    routing = ConversationItem(
        id="rd1",
        type="routing_decision",
        status="completed",
        response_id="turn_1",
        created_at=1,
        data=RoutingDecisionData(model="some-model", applied=True, rationale="cheap"),
    )
    assert render_item_for_excerpt(aside) is None
    assert render_item_for_excerpt(routing) is None


def test_summarizes_tool_calls_and_output() -> None:
    """Tool traffic is included but clipped — it dominates by volume."""
    call = ConversationItem(
        id="fc1",
        type="function_call",
        status="completed",
        response_id="turn_1",
        created_at=1,
        data=FunctionCallData(
            agent="test-agent",
            name="read_file",
            arguments="x" * 900,
            call_id="call_1",
        ),
    )
    rendered = render_item_for_excerpt(call)
    assert rendered is not None
    assert rendered.startswith("assistant called read_file(")
    assert "[truncated]" in rendered
    assert len(rendered) < 900

    output = ConversationItem(
        id="fo1",
        type="function_call_output",
        status="completed",
        response_id="turn_1",
        created_at=1,
        data=FunctionCallOutputData(call_id="call_1", output="ok"),
    )
    assert render_item_for_excerpt(output) == "tool result: ok"


# ── build_transcript_excerpt ─────────────────────────────


def test_excerpt_keeps_chronological_order() -> None:
    """Lines read oldest-first, like the transcript they came from."""
    excerpt = build_transcript_excerpt(
        [
            _message("m1", "user", "first"),
            _message("m2", "assistant", "second"),
            _message("m3", "user", "third"),
        ],
        max_chars=1000,
    )
    assert excerpt.splitlines() == ["user: first", "assistant: second", "user: third"]


def test_excerpt_trims_the_oldest_lines_first() -> None:
    """
    Over budget, the tail survives.

    A question asked mid-session is almost always about what just
    happened, so dropping the head keeps the useful half.
    """
    items = [_message(f"m{i}", "user", f"line {i}") for i in range(20)]
    excerpt = build_transcript_excerpt(items, max_chars=40)
    lines = excerpt.splitlines()
    assert lines[-1] == "user: line 19"
    assert len(excerpt) <= 40
    assert "line 0" not in excerpt


def test_excerpt_is_empty_when_nothing_renders() -> None:
    """A session of pure metadata yields no excerpt rather than junk."""
    assert build_transcript_excerpt([], max_chars=100) == ""
    assert build_transcript_excerpt([_message("m1", "user", "  ")], max_chars=100) == ""


def test_excerpt_keeps_the_tail_of_an_oversized_single_line() -> None:
    """One huge line still yields its most recent slice, not nothing."""
    excerpt = build_transcript_excerpt(
        [_message("m1", "user", "a" * 50 + "END")],
        max_chars=20,
    )
    assert len(excerpt) == 20
    assert excerpt.endswith("END")
