"""Tests for canonical system-instruction composition."""

import json
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.entities import ConversationItem, FunctionCallOutputData
from omnigent.runner.app import _format_subagent_wake_notice
from omnigent.runtime.prompt import (
    SUBAGENT_WAKE_NOTICE_INSTRUCTION,
    SUBAGENT_WAKE_NOTICE_SHAPE,
    append_framework_instructions,
    build_instructions,
    build_instructions_nullable,
    history_to_input_items,
    raw_author_instructions,
)
from omnigent.spec import AgentSpec

_SAMPLE_FRAMEWORK_INSTRUCTION = "Framework instruction for testing build_instructions_nullable."


def _spec(
    instructions: str | None,
    *,
    agents: tuple[str, ...] = (),
    spawn: bool = False,
    builtins: tuple[str, ...] = (),
) -> AgentSpec:
    """
    Stub only the AgentSpec fields the instruction builders read.
    """
    return cast(
        AgentSpec,
        SimpleNamespace(
            instructions=instructions,
            skills=[],
            tools=SimpleNamespace(
                agents=list(agents),
                builtins=[SimpleNamespace(name=name) for name in builtins],
            ),
            spawn=spawn,
        ),
    )


def _output_item(output: str) -> ConversationItem:
    """Build a persisted ``function_call_output`` item for replay tests."""
    return ConversationItem(
        id="i1",
        status="completed",
        response_id="r1",
        created_at=1,
        type="function_call_output",
        data=FunctionCallOutputData(call_id="c1", output=output),
    )


def test_history_replay_strips_inline_base64_image() -> None:
    """A stored image tool result must not replay its base64 as prompt text.

    Older sessions persisted a ``Read`` of an image as a JSON list of
    ``{"type":"image","source":{"type":"base64",...}}`` blocks. Replaying that
    verbatim on resume overflows the context window and wedges compaction, so
    ``history_to_input_items`` strips the base64 to a placeholder.
    """
    huge_b64 = "iVBORw0KGgo" + "A" * 100_000
    stored = json.dumps(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": huge_b64},
            }
        ],
        separators=(",", ":"),
    )

    result = history_to_input_items([_output_item(stored)])

    output = result[0]["output"]
    assert huge_b64 not in output, "base64 image data must not be replayed as text"
    assert "image/png image omitted from history" in output
    assert "re-run the tool call" in output
    assert len(output) < 300


def test_history_replay_strips_truncated_image_block() -> None:
    """Base64 clipped at the store byte cap (invalid JSON) is still stripped.

    Real wedged sessions stored the image output truncated at the
    conversation-store byte cap, leaving the base64 string unterminated — so it
    no longer parses as JSON. The strip must fall back to an in-place rewrite,
    or the exact payloads that wedge resume would slip through unchanged.
    """
    huge_b64 = "iVBORw0KGgo" + "A" * 100_000
    # Mimic the store cap: a valid prefix cut mid-base64, no closing quote/braces.
    truncated = (
        '[{"type":"image","source":{"type":"base64","data":"'
        + huge_b64
        + "…[truncated by conversation-store: item exceeded 245760B cap]"
    )
    # Precondition: this is genuinely not parseable JSON.
    with pytest.raises(ValueError):
        json.loads(truncated)

    result = history_to_input_items([_output_item(truncated)])

    output = result[0]["output"]
    assert huge_b64 not in output, "truncated base64 must not survive replay"
    assert "image omitted from history" in output
    assert len(output) < 300


def test_history_replay_leaves_plain_text_output_unchanged() -> None:
    """Plain-text tool outputs (the common case) pass through untouched."""
    result = history_to_input_items([_output_item("TODO contents")])
    assert result[0]["output"] == "TODO contents"


def test_history_replay_leaves_non_image_json_output_unchanged() -> None:
    """A JSON tool output with no image block is returned byte-for-byte."""
    stored = json.dumps([{"type": "text", "text": "hello"}], separators=(",", ":"))
    result = history_to_input_items([_output_item(stored)])
    assert result[0]["output"] == stored


def test_framework_instructions_append_after_custom_prompts() -> None:
    spec = _spec("Agent prompt")

    result = build_instructions(
        spec,
        "Request prompt",
        [],
        framework_instructions=("  Framework prompt  ",),
    )

    assert result == "Agent prompt\n\nRequest prompt\n\nFramework prompt"


def test_empty_framework_instructions_do_not_change_default() -> None:
    spec = _spec(None)

    assert build_instructions(spec, None, [], framework_instructions=("", "   ")) == (
        "You are a helpful assistant."
    )


def test_framework_only_instructions_use_shared_composer() -> None:
    assert append_framework_instructions(None, ("Rename session",)) == "Rename session"


def test_build_instructions_nullable_neither_authored_nor_framework() -> None:
    """No author text, no framework text → None, not the fabricated fallback."""
    spec = _spec(None)
    assert build_instructions_nullable(spec, None, []) is None


def test_build_instructions_nullable_whitespace_only_treated_as_absent() -> None:
    """Whitespace-only spec.instructions is not real content — matches
    raw_author_instructions' non-empty/non-whitespace gate, so authored_present
    and composed agree on what counts as "authored"."""
    spec = _spec("   \n  ")
    assert build_instructions_nullable(spec, None, []) is None
    result = build_instructions_nullable(
        spec, None, [], framework_instructions=(_SAMPLE_FRAMEWORK_INSTRUCTION,)
    )
    assert result == _SAMPLE_FRAMEWORK_INSTRUCTION


def test_build_instructions_nullable_whitespace_only_per_request_treated_as_absent() -> None:
    """Whitespace-only per_request_instructions is not real content either —
    the same non-empty/non-whitespace gate applies to both instruction
    sources, not just spec.instructions."""
    spec = _spec(None)
    assert build_instructions_nullable(spec, "   \n  ", []) is None
    result = build_instructions_nullable(
        spec, "   \n  ", [], framework_instructions=(_SAMPLE_FRAMEWORK_INSTRUCTION,)
    )
    assert result == _SAMPLE_FRAMEWORK_INSTRUCTION


def test_build_instructions_nullable_authored_present() -> None:
    """Author text present → fully composed authored + framework string."""
    spec = _spec("Agent prompt")
    result = build_instructions_nullable(
        spec, "Request prompt", [], framework_instructions=("Framework prompt",)
    )
    assert result == "Agent prompt\n\nRequest prompt\n\nFramework prompt"


def test_build_instructions_nullable_framework_only_omits_fallback() -> None:
    """Framework-only text must never carry the fabricated fallback fused onto it.

    Regression: naively comparing ``build_instructions()``'s output against
    the fallback literal misses this exact case, because
    ``build_instructions`` seeds the fallback as ``base_instructions`` and
    then appends framework text on top of it regardless of whether ``parts``
    was empty — producing a mixed string that is neither the bare literal
    nor framework-text-alone.
    """
    spec = _spec(None)
    result = build_instructions_nullable(
        spec, None, [], framework_instructions=(_SAMPLE_FRAMEWORK_INSTRUCTION,)
    )
    assert result == _SAMPLE_FRAMEWORK_INSTRUCTION
    assert "You are a helpful assistant." not in (result or "")

    # The comparison this helper replaces would have misclassified the
    # framework-only case: build_instructions()'s actual output IS fused
    # with the fallback literal, confirming the unsafe-comparison rationale.
    fused = build_instructions(
        spec, None, [], framework_instructions=(_SAMPLE_FRAMEWORK_INSTRUCTION,)
    )
    assert fused.startswith("You are a helpful assistant.")
    assert _SAMPLE_FRAMEWORK_INSTRUCTION in fused


@pytest.mark.parametrize(
    ("agents", "spawn", "builtins"),
    [(("researcher",), False, ()), ((), True, ()), ((), False, ("web_fetch",))],
)
def test_subagent_wake_instruction_added_for_dispatching_agents(
    agents: tuple[str, ...], spawn: bool, builtins: tuple[str, ...]
) -> None:
    """
    An agent that can dispatch sub-agents is told what a wake notice is.

    The gate mirrors ``sys_session_send`` registration (declared sub-agents or
    ``spawn: true``) plus the ``web_fetch`` builtin, whose researcher dispatch
    wakes the parent the same way. The announcement lands after the authored
    text and before per-turn framework instructions, and on its own it never
    drags in the fabricated fallback.
    """
    dispatching = _spec("Agent prompt", agents=agents, spawn=spawn, builtins=builtins)
    result = build_instructions(dispatching, None, [], framework_instructions=("Turn note",))
    assert result == f"Agent prompt\n\n{SUBAGENT_WAKE_NOTICE_INSTRUCTION}\n\nTurn note"

    unauthored = _spec(None, agents=agents, spawn=spawn, builtins=builtins)
    assert build_instructions_nullable(unauthored, None, []) == SUBAGENT_WAKE_NOTICE_INSTRUCTION


def test_subagent_wake_notice_shape_matches_runner_notice() -> None:
    """
    The announced shape must track the notice the runner actually posts.
    """
    expected = (
        SUBAGENT_WAKE_NOTICE_SHAPE.replace("<agent>/<title>", "researcher/auth")
        .replace("<status>", "completed")
        .replace("<N>", "2")
    )
    notice = _format_subagent_wake_notice(
        agent="researcher", title="auth", status="completed", pending=2
    )
    assert notice == expected


def test_raw_author_instructions_verbatim_and_none() -> None:
    present = cast(AgentSpec, SimpleNamespace(instructions="  Keep this exact.  "))
    assert raw_author_instructions(present) == "  Keep this exact.  "

    absent = cast(AgentSpec, SimpleNamespace(instructions=None))
    assert raw_author_instructions(absent) is None

    whitespace_only = cast(AgentSpec, SimpleNamespace(instructions="   \n  "))
    assert raw_author_instructions(whitespace_only) is None
