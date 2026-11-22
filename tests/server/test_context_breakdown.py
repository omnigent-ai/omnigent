"""Unit tests for the session context-window breakdown estimates."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.entities import ConversationItem
from omnigent.entities.conversation import (
    COMPACTION_SUMMARY_REPLAY_PROMPT,
    CompactionData,
    FunctionCallData,
    FunctionCallOutputData,
    ItemData,
    MessageData,
    NativeToolData,
    ReasoningData,
    external_item_id,
)
from omnigent.entities.file import StoredFile
from omnigent.mcp_selection import MCP_SELECTION_PREFIX
from omnigent.runner.session_history import build_session_history
from omnigent.runtime.context_breakdown import IMAGE_TOKEN_ESTIMATE, estimate_text_tokens
from omnigent.runtime.prompt import (
    build_instructions,
    build_instructions_nullable,
    build_native_startup_instructions,
    dedupe_tool_output_images,
)
from omnigent.server.context_breakdown import (
    HistoryEstimate,
    build_context_breakdown,
    scan_history,
)
from omnigent.server.schemas import (
    ContextBreakdownCategory,
    ContextBreakdownMeasured,
    SessionContextBreakdown,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig, SkillSpec


def _item(item_id: str, item_type: str, data: ItemData) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type=item_type,
        status="completed",
        response_id="resp_1",
        created_at=1000,
        data=data,
    )


def _user(item_id: str, *blocks: dict[str, object]) -> ConversationItem:
    return _item(item_id, "message", MessageData(role="user", content=list(blocks)))


def _assistant(item_id: str, text: str) -> ConversationItem:
    return _item(
        item_id,
        "message",
        MessageData(role="assistant", agent="a", content=[{"type": "output_text", "text": text}]),
    )


def _text(text: str) -> dict[str, object]:
    return {"type": "input_text", "text": text}


def _category(breakdown: SessionContextBreakdown, kind: str) -> ContextBreakdownCategory:
    (category,) = [row for row in breakdown.categories if row.kind == kind]
    return category


def _kinds(breakdown: SessionContextBreakdown) -> list[str]:
    return [row.kind for row in breakdown.categories]


def test_estimate_text_tokens_rounds_utf8_bytes_up() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcde") == 2
    # Four 3-byte characters are 12 bytes.
    assert estimate_text_tokens("日本語字") == 3


def test_scan_history_counts_messages_tool_calls_and_skips_reasoning() -> None:
    items = [
        _user("msg_1", _text("a" * 40)),
        _item(
            "fc_1",
            "function_call",
            FunctionCallData(agent="a", name="grep", arguments="b" * 36, call_id="c1"),
        ),
        _item(
            "fco_1", "function_call_output", FunctionCallOutputData(call_id="c1", output="c" * 80)
        ),
        _item("rs_1", "reasoning", ReasoningData(agent="a", summary=[{"text": "d" * 400}])),
        _assistant("msg_2", "e" * 20),
    ]

    history, attachments = scan_history(reversed(items))

    assert history.item_count == 4
    assert history.estimated_tokens == 10 + 10 + 20 + 5
    assert history.summarized_item_count == 0
    assert history.summary_tokens == 0
    assert history.partial is False
    assert attachments == []


def test_scan_history_counts_inline_tool_output_images_as_images() -> None:
    prefix = 'see [{"image_url": "'
    suffix = '"}]'
    output = prefix + "data:image/png;base64," + "A" * 4_000 + suffix
    items = [
        _item("fco_1", "function_call_output", FunctionCallOutputData(call_id="c", output=output))
    ]

    history, _ = scan_history(items)

    assert history.estimated_tokens == (
        estimate_text_tokens(prefix + suffix) + IMAGE_TOKEN_ESTIMATE
    )


def test_scan_history_sizes_stored_tool_images_as_their_replay_placeholder() -> None:
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png"}}
    block["source"]["data"] = "A" * 400_000
    output = json.dumps([block])
    items = [
        _item("fco_1", "function_call_output", FunctionCallOutputData(call_id="c", output=output))
    ]

    history, _ = scan_history(items)

    assert history.estimated_tokens == estimate_text_tokens(dedupe_tool_output_images(output))
    assert history.estimated_tokens < 200


def test_scan_history_counts_a_generated_image_once() -> None:
    item = {"type": "image_generation_call", "id": "ig_1", "result": "A" * 400_000}
    items = [_item("nt_1", "native_tool", NativeToolData(item=item))]

    history, _ = scan_history(items, native=True)

    rest = estimate_text_tokens(json.dumps({"type": "image_generation_call", "id": "ig_1"}))
    assert history.estimated_tokens == rest + IMAGE_TOKEN_ESTIMATE
    # The runner's turn replay does not send provider tool items.
    assert scan_history(items)[0].item_count == 0


def _runner_replay(items: list[ConversationItem]) -> list[dict[str, object]]:
    """The input the runner's turn replay builds from *items* (oldest first)."""
    session_history = build_session_history(
        _background_tasks=set(),
        _last_server_item_id={},
        _persist_cancellation_items=AsyncMock(),
        _session_histories={},
        _session_spec_cache={},
        server_client=MagicMock(spec=httpx.AsyncClient),
    )
    return session_history.convert_raw_items_to_input([item.to_api_dict() for item in items])


def _replayed_live_ids(items: list[ConversationItem]) -> list[object]:
    """Stored messages (by id) and tool records (by call id) the runner replays."""
    stored = {item.id for item in items}
    compactions = [item.data for item in items if isinstance(item.data, CompactionData)]
    # Skip the entries the latest checkpoint itself replays.
    checkpoint_entries = (
        0 if not compactions else len(compactions[-1].compacted_messages or []) or 2
    )
    replayed: list[object] = []
    for entry in _runner_replay(items)[checkpoint_entries:]:
        if entry.get("type") in ("function_call", "function_call_output"):
            replayed.append(f"{entry['type']}:{entry.get('call_id')}")
        elif entry.get("id") in stored:
            replayed.append(entry.get("id"))
    return replayed


# Every summary-only checkpoint replays this prompt before its summary.
_REPLAY_PROMPT_TOKENS = estimate_text_tokens(COMPACTION_SUMMARY_REPLAY_PROMPT)


def test_scan_history_replaces_compacted_items_with_the_latest_summary() -> None:
    items = [
        _user("msg_1", _text("x" * 4_000)),
        _assistant("msg_2", "x" * 4_000),
        _item(
            "cmp_old",
            "compaction",
            CompactionData(summary="old", last_item_id="msg_1", token_count=999),
        ),
        _user("msg_3", _text("y" * 4_000)),
        # After the anchor but before the compaction record: replay drops it.
        _user("msg_4", _text("z" * 40), {"type": "input_image", "file_id": "file_gap"}),
        _item(
            "cmp_new",
            "compaction",
            CompactionData(summary="new", last_item_id="msg_3", token_count=50),
        ),
        _user("msg_5", _text("w" * 40), {"type": "input_image", "file_id": "file_live"}),
        _item(
            "fc_1",
            "function_call",
            FunctionCallData(agent="a", name="grep", arguments="b" * 36, call_id="c1"),
        ),
        _item(
            "fco_1", "function_call_output", FunctionCallOutputData(call_id="c1", output="c" * 80)
        ),
        _item("nt_1", "native_tool", NativeToolData(item={"type": "web_search_call"})),
    ]

    history, attachments = scan_history(reversed(items))

    assert _replayed_live_ids(items) == [
        "msg_5",
        "function_call:c1",
        "function_call_output:c1",
    ]
    assert history.item_count == 3
    assert history.summarized_item_count == 4
    summary = _REPLAY_PROMPT_TOKENS + estimate_text_tokens("new")
    assert history.summary_tokens == summary
    assert history.estimated_tokens == summary + 10 + 10 + 20
    assert [ref.file_id for ref in attachments] == ["file_live"]


def test_scan_history_marks_a_capped_scan_partial() -> None:
    items = [_assistant(f"msg_{index}", "abcd") for index in range(5)]

    history, _ = scan_history(reversed(items), max_items=3)

    assert history.partial is True
    assert history.summarized_partial is False
    assert history.item_count == 3


def test_scan_history_keeps_live_counts_exact_when_the_cap_lands_in_the_summary() -> None:
    items = [
        *(_assistant(f"old_{index}", "abcd") for index in range(5)),
        _item(
            "cmp", "compaction", CompactionData(summary="s", last_item_id="old_4", token_count=9)
        ),
        _assistant("live", "abcd"),
    ]

    history, _ = scan_history(reversed(items), max_items=4)

    assert history.partial is False
    assert history.summarized_partial is True
    assert history.item_count == 1
    assert history.summarized_item_count == 2


@pytest.mark.parametrize(
    ("summary", "anchor"),
    [("", "msg_1"), ("summary", ""), ("summary", "synthetic_abc")],
    ids=["empty-summary", "empty-anchor", "synthetic-anchor"],
)
def test_scan_history_replays_any_latest_compaction_like_the_runner(
    summary: str, anchor: str
) -> None:
    items = [
        _user("msg_1", _text("a" * 40)),
        _item(
            "cmp_old",
            "compaction",
            CompactionData(summary="ok", last_item_id="msg_1", token_count=5),
        ),
        _item(
            "cmp",
            "compaction",
            CompactionData(summary=summary, last_item_id=anchor, token_count=7),
        ),
        _assistant("msg_2", "b" * 40),
    ]

    history, _ = scan_history(reversed(items))

    # The runner's turn replay does not validate the latest compaction.
    assert _replayed_live_ids(items) == ["msg_2"]
    assert history.item_count == 1
    assert history.summarized_item_count == 1
    summary_tokens = _REPLAY_PROMPT_TOKENS + estimate_text_tokens(summary)
    assert history.summary_tokens == summary_tokens
    assert history.estimated_tokens == summary_tokens + 10


def test_scan_history_sizes_a_native_summary_without_a_token_count() -> None:
    items = [
        _user("msg_1", _text("a" * 40)),
        _item(
            "cmp",
            "compaction",
            CompactionData(summary="s" * 400, last_item_id="msg_1", token_count=0),
        ),
    ]

    history, _ = scan_history(reversed(items))

    assert history.summary_tokens == _REPLAY_PROMPT_TOKENS + 100
    assert history.estimated_tokens == _REPLAY_PROMPT_TOKENS + 100

    # A forwarder-shaped checkpoint: placeholder summary plus retained messages.
    image = {"type": "input_image", "file_id": "file_img"}
    function_call = {"type": "function_call", "call_id": "c1", "name": "grep"}
    function_call["arguments"] = "a" * 400
    checkpoint = CompactionData(
        summary="[Claude Code compaction — context was compacted in the terminal]",
        last_item_id="msg_old",
        token_count=0,
        compacted_messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "r" * 400}, image],
                "forwarded_source_ids": ["rec-1"],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "k" * 40_000},
                    # Prior-turn thinking is not input context.
                    {"type": "thinking", "thinking": "t" * 400, "signature": "s" * 8_000},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                    },
                    {"type": "document", "source": {"type": "text", "data": "d" * 400}},
                ],
            },
            {**function_call, "id": "fc_snapshot"},
            {"role": "assistant", "content": "p" * 40},
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": "https://x.test/a"}}
                ],
            },
        ],
    )
    covered_id = external_item_id("conv_1", "rec-1")
    items = [
        _user("msg_old", _text("o" * 4_000)),
        # Written between the forwarder's read and its checkpoint POST.
        _user("msg_gap", _text("g" * 400)),
        _item("cmp", "compaction", checkpoint),
        # Forwarded after the checkpoint, which already carries it.
        _user(covered_id, _text("r" * 400), image),
        # A later summary-only checkpoint, which native replay skips.
        _item(
            "cmp_fallback",
            "compaction",
            CompactionData(summary="placeholder", last_item_id=covered_id, token_count=0),
        ),
        _assistant("msg_new", "n" * 40),
    ]
    stored_image = _stored("file_img", "shot.png", "image/png", 900)

    def breakdown_for(harness: str) -> SessionContextBreakdown:
        return build_context_breakdown(
            "conv_1",
            spec=None,
            harness=harness,
            labels={},
            items_newest_first=reversed(items),
            lookup_file={"file_img": stored_image}.get,
        )

    def history_for(harness: str) -> HistoryEstimate:
        history, _ = scan_history(
            reversed(items), session_id="conv_1", native=harness == "claude-native"
        )
        return history

    native = breakdown_for("claude-native")
    native_history = history_for("claude-native")
    checkpoint_tokens = 100 + 10_000 + 100 + estimate_text_tokens(json.dumps(function_call)) + 10
    # The retained messages, not the placeholder summary, size the checkpoint.
    assert native_history.summary_tokens == checkpoint_tokens
    # Native replay drops everything before the checkpoint and the item it carries.
    assert native_history.item_count == 1
    assert native_history.summarized_item_count == 2
    assert native_history.estimated_tokens == checkpoint_tokens + 10
    history_row = _category(native, "history")
    assert (history_row.tokens, history_row.source, history_row.count) == (
        checkpoint_tokens + 10,
        "estimated",
        1,
    )
    assert history_row.detail == ("1 message or tool call · 2 earlier items replaced by a summary")
    # The retained image is listed once; the snapshot-stripped one was not delivered.
    rows = [(row.filename, row.delivery, row.estimated_tokens) for row in native.attachments]
    assert rows == [
        ("shot.png", "workspace", None),
        (None, "missing", None),
        (None, "inline", IMAGE_TOKEN_ESTIMATE),
    ]

    # The runner replays the latest compaction, summary-only or not, in place of all before it.
    runtime = history_for("claude-sdk")
    assert runtime.summary_tokens == _REPLAY_PROMPT_TOKENS + estimate_text_tokens("placeholder")
    assert runtime.item_count == 1
    assert runtime.summarized_item_count == 3

    # Without it, the runner replays the retained messages and every item after them.
    items = [item for item in items if item.id != "cmp_fallback"]
    runtime = history_for("claude-sdk")
    assert runtime.summary_tokens == checkpoint_tokens
    assert _replayed_live_ids(items) == [covered_id, "msg_new"]
    assert runtime.item_count == 2
    assert runtime.summarized_item_count == 2


def _stored(
    file_id: str, filename: str, content_type: str, size: int, **metadata: str
) -> StoredFile:
    return StoredFile(
        id=file_id,
        created_at=1000,
        filename=filename,
        bytes=size,
        content_type=content_type,
        source_metadata=metadata or None,
    )


def test_breakdown_reports_attachment_delivery_and_estimates() -> None:
    files = {
        "file_img": _stored("file_img", "shot.png", "image/png", 900_000),
        "file_txt": _stored("file_txt", "notes.md", "text/markdown", 4_001),
        "file_pdf": _stored("file_pdf", "spec.pdf", "application/pdf", 80_000),
        "file_zip": _stored(
            "file_zip", "logs.zip", "application/zip", 5_000, delivery="filesystem"
        ),
    }
    items = [
        _user(
            "msg_1",
            {"type": "input_image", "file_id": "file_img", "filename": "shot.png"},
            {"type": "input_file", "file_id": "file_txt", "filename": "notes.md"},
            _text("look"),
        ),
        _user(
            "msg_2",
            {"type": "input_file", "file_id": "file_pdf"},
            {"type": "input_file", "file_id": "file_zip"},
            {"type": "input_file", "file_id": "file_gone", "filename": "old.csv"},
            {"type": "input_file", "filename": "inline.txt", "file_data": "QUJDRA=="},
            {"type": "input_image", "image_url": "https://example.test/cat.png"},
        ),
    ]

    breakdown = build_context_breakdown(
        "conv_1",
        spec=None,
        harness=None,
        labels={},
        items_newest_first=reversed(items),
        lookup_file=files.get,
    )

    rows = [
        (row.filename, row.delivery, row.bytes, row.estimated_tokens)
        for row in breakdown.attachments
    ]
    assert rows == [
        ("shot.png", "inline", 900_000, IMAGE_TOKEN_ESTIMATE),
        ("notes.md", "inline", 4_001, 1_001),
        ("spec.pdf", "inline", 80_000, None),
        ("logs.zip", "workspace", 5_000, None),
        ("old.csv", "missing", None, None),
        ("inline.txt", "inline", 4, 1),
        (None, "inline", None, IMAGE_TOKEN_ESTIMATE),
    ]
    # Attachments are reported separately, not inside the history estimate.
    assert _category(breakdown, "history").tokens == 1
    attachments = _category(breakdown, "attachments")
    assert (attachments.tokens, attachments.count, attachments.source) == (
        IMAGE_TOKEN_ESTIMATE + 1_001 + 1 + IMAGE_TOKEN_ESTIMATE,
        7,
        "estimated",
    )
    assert attachments.detail == "Plus 1 sent without an estimate, counted in Other"
    # Without a spec the prompt row has no figure, and without a report no remainder.
    prompt = _category(breakdown, "system_prompt")
    assert (prompt.tokens, prompt.source) == (None, "unknown")
    assert _kinds(breakdown) == ["system_prompt", "tools", "attachments", "history"]
    assert breakdown.measured is None
    assert breakdown.bytes_per_token == 4


def _spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        instructions="You review pull requests carefully." * 10,
        skills=[SkillSpec(name="triage", description="Sort incoming issues.", content="body")],
        mcp_servers=[
            MCPServerConfig(name="github", url="https://example.test/mcp"),
            MCPServerConfig(name="slack", transport="registry"),
        ],
    )


def test_breakdown_estimates_the_composed_prompt_and_lists_selected_tools() -> None:
    spec = _spec()

    breakdown = build_context_breakdown(
        "conv_1",
        spec=spec,
        harness="claude-sdk",
        labels={f"{MCP_SELECTION_PREFIX}slack": "0", f"{MCP_SELECTION_PREFIX}linear": "1"},
        items_newest_first=[],
        lookup_file=lambda _file_id: None,
    )

    prompt = _category(breakdown, "system_prompt")
    assert prompt.tokens == estimate_text_tokens(build_instructions(spec, None, []))
    assert (prompt.source, prompt.origin) == ("estimated", "server")
    assert prompt.detail == "Agent instructions and Omnigent guidance"
    # The runner composes without tool schemas, so the prompt carries no skill listing.
    assert _kinds(breakdown) == ["system_prompt", "tools", "history"]
    tools = _category(breakdown, "tools")
    assert (tools.tokens, tools.source) == (None, "unknown")
    assert tools.detail == "MCP: github, linear"
    history = _category(breakdown, "history")
    assert (history.tokens, history.count, history.detail) == (0, 0, "0 messages and tool calls")


_LOAD_SKILL_SCHEMA = {"type": "function", "function": {"name": "load_skill"}}


def test_breakdown_lists_skills_only_when_the_prompt_carries_the_listing() -> None:
    spec = _spec()

    def breakdown_with(tool_schemas: list[dict[str, object]]) -> SessionContextBreakdown:
        return build_context_breakdown(
            "conv_1",
            spec=spec,
            harness="openai-agents",
            labels={},
            items_newest_first=[],
            lookup_file=lambda _file_id: None,
            tool_schemas=tool_schemas,
        )

    without = breakdown_with([])
    assert "skills" not in _kinds(without)

    injected = breakdown_with([_LOAD_SKILL_SCHEMA])
    skills = _category(injected, "skills")
    listing = (
        "Available skills (use the load_skill tool to load one):\n- triage: Sort incoming issues."
    )
    assert (skills.tokens, skills.count, skills.source) == (
        estimate_text_tokens(listing),
        1,
        "estimated",
    )
    # The listing is part of the composed prompt, so the two rows add up to it.
    composed = build_instructions(spec, None, [_LOAD_SKILL_SCHEMA])
    assert listing in composed
    prompt_tokens = _category(injected, "system_prompt").tokens
    assert prompt_tokens is not None
    assert prompt_tokens + (skills.tokens or 0) == estimate_text_tokens(composed)
    # A startup channel never carries the listing, whatever tools the turn lists.
    native = build_context_breakdown(
        "conv_1",
        spec=spec,
        harness="claude-native",
        labels={},
        items_newest_first=[],
        lookup_file=lambda _file_id: None,
        tool_schemas=[_LOAD_SKILL_SCHEMA],
    )
    assert "skills" not in _kinds(native)


@pytest.mark.parametrize(
    ("harness", "tokens_of", "source", "detail"),
    [
        # composed-per-turn and session-snapshot harnesses get the turn instructions.
        ("openai-agents", "composed", "estimated", "Agent instructions and Omnigent guidance"),
        ("claude-sdk", "composed", "estimated", "Agent instructions and Omnigent guidance"),
        ("opencode-native", "composed", "estimated", "Agent instructions and Omnigent guidance"),
        # A startup channel carries the native startup form.
        ("codex-native", "startup", "estimated", "Agent instructions and Omnigent guidance"),
        (
            "cursor",
            "composed",
            "estimated",
            "Agent instructions and Omnigent guidance, sent with the first message",
        ),
        ("cursor-native", None, "unknown", "Not delivered by this harness"),
        ("kimi", None, "unknown", "Not delivered by this harness"),
        # An in-process harness that declares no mode is still handed the instructions.
        ("agents_sdk", "composed", "estimated", "Agent instructions and Omnigent guidance"),
    ],
)
def test_breakdown_system_prompt_follows_the_harness_instruction_delivery(
    harness: str, tokens_of: str | None, source: str, detail: str
) -> None:
    spec = _spec()
    expected = {
        "composed": estimate_text_tokens(build_instructions(spec, None, [])),
        "startup": estimate_text_tokens(build_native_startup_instructions(spec)),
        None: None,
    }[tokens_of]

    breakdown = build_context_breakdown(
        "conv_1",
        spec=spec,
        harness=harness,
        labels={},
        items_newest_first=[],
        lookup_file=lambda _file_id: None,
        measured=ContextBreakdownMeasured(total_tokens=10_000, context_window=200_000),
    )

    prompt = _category(breakdown, "system_prompt")
    assert (prompt.tokens, prompt.source, prompt.detail) == (expected, source, detail)
    # An undelivered prompt is not itemized, so the remainder keeps the whole total.
    assert _category(breakdown, "other").tokens == 10_000 - (expected or 0)


def test_breakdown_gated_harness_omits_the_fallback_line_for_an_empty_spec() -> None:
    bare = AgentSpec(spec_version=1)

    def prompt_tokens(harness: str) -> int | None:
        breakdown = build_context_breakdown(
            "conv_1",
            spec=bare,
            harness=harness,
            labels={},
            items_newest_first=[],
            lookup_file=lambda _file_id: None,
        )
        return _category(breakdown, "system_prompt").tokens

    # A gated harness gets the nullable form; the others get the fallback line too.
    assert prompt_tokens("opencode-native") == estimate_text_tokens(
        build_instructions_nullable(bare, None, []) or ""
    )
    assert prompt_tokens("openai-agents") == estimate_text_tokens(
        build_instructions(bare, None, [])
    )
    assert prompt_tokens("opencode-native") != prompt_tokens("openai-agents")


def _measured_breakdown(
    total_tokens: int | None, *, context_window: int | None = 200_000, text_bytes: int = 4_000
) -> SessionContextBreakdown:
    return build_context_breakdown(
        "conv_1",
        spec=None,
        harness="claude-sdk",
        labels={},
        items_newest_first=[_user("msg_1", _text("a" * text_bytes))],
        lookup_file=lambda _file_id: None,
        measured=(
            None
            if total_tokens is None
            else ContextBreakdownMeasured(total_tokens=total_tokens, context_window=context_window)
        ),
    )


def test_breakdown_derives_the_remainder_from_the_measured_total() -> None:
    breakdown = _measured_breakdown(5_000)

    assert breakdown.measured == ContextBreakdownMeasured(
        total_tokens=5_000, context_window=200_000
    )
    assert _kinds(breakdown) == ["system_prompt", "tools", "history", "other", "free"]
    other = _category(breakdown, "other")
    # The history estimate is 1,000 tokens; rows without a figure count as nothing.
    assert (other.tokens, other.source) == (4_000, "estimated")
    # The spec did not load, so the instructions are part of the remainder too.
    assert other.detail == (
        "Instructions, tool schemas, content the harness adds itself and the last reply"
    )
    free = _category(breakdown, "free")
    assert (free.tokens, free.source) == (195_000, "measured")
    assert breakdown.estimates_exceed_measured is False


def test_breakdown_without_a_measured_total_has_no_remainder_rows() -> None:
    breakdown = _measured_breakdown(None)

    assert breakdown.measured is None
    assert _kinds(breakdown) == ["system_prompt", "tools", "history"]
    assert breakdown.estimates_exceed_measured is False


def test_breakdown_flags_estimates_that_exceed_the_measured_total() -> None:
    breakdown = _measured_breakdown(600)

    assert breakdown.estimates_exceed_measured is True
    other = _category(breakdown, "other")
    assert other.tokens == 0
    assert other.detail == "The estimates above add up to more than the last measured total"
    # The itemized row keeps its own estimate rather than being scaled to fit.
    assert _category(breakdown, "history").tokens == 1_000


def test_breakdown_omits_free_space_when_the_window_is_unknown() -> None:
    breakdown = _measured_breakdown(5_000, context_window=None)

    assert _kinds(breakdown) == ["system_prompt", "tools", "history", "other"]


def test_breakdown_leaves_history_unsized_for_a_native_harness_with_its_own_rules() -> None:
    items = [_user("msg_1", _text("a" * 4_000)), _assistant("msg_2", "b" * 40)]

    def history_row(harness: str) -> ContextBreakdownCategory:
        breakdown = build_context_breakdown(
            "conv_1",
            spec=None,
            harness=harness,
            labels={},
            items_newest_first=reversed(items),
            lookup_file=lambda _file_id: None,
            measured=ContextBreakdownMeasured(total_tokens=5_000, context_window=200_000),
        )
        assert _category(breakdown, "other").tokens == 5_000 - (
            _category(breakdown, "history").tokens or 0
        )
        return _category(breakdown, "history")

    sized = history_row("claude-native")
    assert (sized.tokens, sized.source) == (1_010, "estimated")
    unsized = history_row("codex-native")
    assert (unsized.tokens, unsized.source, unsized.count) == (None, "unknown", 2)
    assert unsized.detail == (
        "2 messages and tool calls stored; this harness keeps its own history"
    )


def test_breakdown_remainder_names_every_row_it_absorbs() -> None:
    breakdown = build_context_breakdown(
        "conv_1",
        spec=_spec(),
        harness="cursor-native",
        labels={},
        items_newest_first=[_user("msg_1", _text("a" * 4_000))],
        lookup_file=lambda _file_id: None,
        measured=ContextBreakdownMeasured(total_tokens=5_000, context_window=200_000),
    )

    # No row carries a figure on this harness, so the remainder is the whole total.
    other = _category(breakdown, "other")
    assert other.tokens == 5_000
    assert other.detail == (
        "Instructions, tool schemas, conversation history, content the harness adds itself"
        " and the last reply"
    )


def test_breakdown_reflects_native_harness_prompt_and_attachment_paths() -> None:
    spec = _spec()
    image = _stored("file_img", "shot.png", "image/png", 900)
    items = [
        _user(
            "msg_1",
            {"type": "input_image", "file_id": "file_img"},
            {
                "type": "input_image",
                "filename": "paste.png",
                "image_url": "data:image/png;base64,QUJDRA==",
            },
            {"type": "input_image", "filename": "remote.png", "image_url": "https://x.test/a.png"},
            {"type": "input_file", "filename": "bare.txt", "file_data": "QUJDRA=="},
            {
                "type": "input_image",
                "filename": "broken.png",
                "image_url": "data:image/png;base64,A",
            },
            {
                "type": "input_image",
                "filename": "empty.png",
                "image_url": "data:image/png;base64,",
            },
            {
                "type": "input_image",
                "filename": "nopad.png",
                "image_url": "data:image/png;base64,QUJDRA",
            },
            {
                "type": "input_file",
                "filename": "report.pdf",
                "file_data": "data:application/pdf;base64,QUJDRA==",
            },
            {
                "type": "input_file",
                "filename": "broken.pdf",
                "file_data": "data:application/pdf;base64,A",
            },
            {"type": "input_file", "filename": "plain.txt", "file_data": "data:text/plain,"},
            {
                "type": "input_image",
                "filename": "..",
                "image_url": "data:image/png;base64,QUJDRA==",
            },
        )
    ]

    breakdown = build_context_breakdown(
        "conv_1",
        spec=spec,
        harness="claude-native",
        labels={},
        items_newest_first=items,
        lookup_file={"file_img": image}.get,
    )

    assert _category(breakdown, "system_prompt").tokens == estimate_text_tokens(
        build_native_startup_instructions(spec)
    )
    # A native CLI receives every attachment as a path, uploaded or sent as a data URI.
    rows = [
        (row.filename, row.delivery, row.bytes, row.estimated_tokens)
        for row in breakdown.attachments
    ]
    assert rows == [
        ("shot.png", "workspace", 900, None),
        ("paste.png", "workspace", 4, None),
        # Only data URIs materialize, so these reach the CLI as "could not be loaded".
        ("remote.png", "missing", None, None),
        ("bare.txt", "missing", 4, None),
        # A data URI the native decoder rejects never becomes a file.
        ("broken.png", "missing", None, None),
        ("empty.png", "missing", None, None),
        ("nopad.png", "missing", 4, None),
        ("report.pdf", "workspace", 4, None),
        ("broken.pdf", "missing", None, None),
        ("plain.txt", "missing", None, None),
        # The harness refuses a name that resolves to the parent directory.
        ("..", "missing", 4, None),
    ]


def test_breakdown_lists_declared_servers_when_a_selection_label_is_malformed() -> None:
    breakdown = build_context_breakdown(
        "conv_1",
        spec=_spec(),
        harness="claude-sdk",
        labels={f"{MCP_SELECTION_PREFIX}slack": "maybe"},
        items_newest_first=[],
        lookup_file=lambda _file_id: None,
    )

    assert _category(breakdown, "tools").detail == "MCP: github, slack"
