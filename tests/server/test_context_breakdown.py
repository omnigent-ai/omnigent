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
from omnigent.runtime.prompt import (
    build_instructions_nullable,
    build_native_startup_instructions,
    dedupe_tool_output_images,
)
from omnigent.server.context_breakdown import (
    FAILED_BREAKDOWN_LABEL_KEY,
    IMAGE_TOKEN_ESTIMATE,
    REPORTED_BREAKDOWN_LABEL_KEY,
    build_context_breakdown,
    estimate_text_tokens,
    failed_turn_breakdown_labels,
    scan_history,
)
from omnigent.server.schemas import SessionContextBreakdown
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

    native = breakdown_for("claude-native")
    checkpoint_tokens = 100 + 10_000 + 100 + estimate_text_tokens(json.dumps(function_call)) + 10
    # The retained messages, not the placeholder summary, size the checkpoint.
    assert native.history.summary_tokens == checkpoint_tokens
    # Native replay drops everything before the checkpoint and the item it carries.
    assert native.history.item_count == 1
    assert native.history.summarized_item_count == 2
    assert native.history.estimated_tokens == checkpoint_tokens + 10
    # The retained image is listed once; the snapshot-stripped one was not delivered.
    rows = [(row.filename, row.delivery, row.estimated_tokens) for row in native.attachments]
    assert rows == [
        ("shot.png", "workspace", None),
        (None, "missing", None),
        (None, "inline", IMAGE_TOKEN_ESTIMATE),
    ]

    # The runner replays the latest compaction, summary-only or not, in place of all before it.
    runtime = breakdown_for("claude-sdk")
    assert runtime.history.summary_tokens == _REPLAY_PROMPT_TOKENS + estimate_text_tokens(
        "placeholder"
    )
    assert runtime.history.item_count == 1
    assert runtime.history.summarized_item_count == 3

    # Without it, the runner replays the retained messages and every item after them.
    items = [item for item in items if item.id != "cmp_fallback"]
    runtime = breakdown_for("claude-sdk")
    assert runtime.history.summary_tokens == checkpoint_tokens
    assert _replayed_live_ids(items) == [covered_id, "msg_new"]
    assert runtime.history.item_count == 2
    assert runtime.history.summarized_item_count == 2


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
    assert breakdown.history.estimated_tokens == 1
    assert breakdown.system_prompt_tokens is None
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


def test_breakdown_estimates_spec_prompt_skills_and_selected_tools() -> None:
    spec = _spec()

    breakdown = build_context_breakdown(
        "conv_1",
        spec=spec,
        harness="claude-sdk",
        labels={f"{MCP_SELECTION_PREFIX}slack": "0", f"{MCP_SELECTION_PREFIX}linear": "1"},
        items_newest_first=[],
        lookup_file=lambda _file_id: None,
    )

    composed = build_instructions_nullable(spec, None, [])
    assert composed is not None
    assert breakdown.system_prompt_tokens == estimate_text_tokens(composed)
    assert breakdown.skill_count == 1
    assert breakdown.skill_listing_tokens == estimate_text_tokens(
        "- triage: Sort incoming issues."
    )
    assert breakdown.tools.mcp_servers == ["github", "linear"]
    assert breakdown.history.item_count == 0


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

    assert breakdown.system_prompt_tokens == estimate_text_tokens(
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

    assert breakdown.tools.mcp_servers == ["github", "slack"]


def test_breakdown_returns_the_runner_reports_stored_on_the_session() -> None:
    """The runner's last-turn report and a later overflow attempt ride on the response."""
    labels = {
        REPORTED_BREAKDOWN_LABEL_KEY: '{"system_tokens":1200,"tool_tokens":5400,"tool_count":18}',
        **failed_turn_breakdown_labels(
            {
                "code": "context_length_exceeded",
                "message": "Context window exceeded",
                "context_breakdown": {"history_tokens": 190_000, "history_item_count": 300},
            }
        ),
    }

    breakdown = build_context_breakdown(
        "conv_1",
        spec=None,
        harness="claude-sdk",
        labels=labels,
        items_newest_first=[],
        lookup_file=lambda _file_id: None,
    )

    assert breakdown.reported is not None
    assert (breakdown.reported.tool_tokens, breakdown.reported.tool_count) == (5400, 18)
    assert breakdown.failed_attempt is not None
    assert breakdown.failed_attempt.history_tokens == 190_000
    assert breakdown.failed_attempt.total_tokens == 190_000


@pytest.mark.parametrize(
    "error",
    [
        # Only an overflow records its attempt.
        {"code": "rate_limit_exceeded", "message": "slow down", "context_breakdown": {}},
        {"code": "context_length_exceeded", "message": "too long"},
        {
            "code": "context_length_exceeded",
            "message": "x",
            "context_breakdown": {"tool_count": -1},
        },
        None,
    ],
)
def test_failed_attempt_labels_need_an_overflow_with_a_valid_breakdown(error: object) -> None:
    assert failed_turn_breakdown_labels(error) == {}


def test_breakdown_ignores_a_malformed_stored_report() -> None:
    breakdown = build_context_breakdown(
        "conv_1",
        spec=None,
        harness=None,
        labels={REPORTED_BREAKDOWN_LABEL_KEY: "{not json", FAILED_BREAKDOWN_LABEL_KEY: ""},
        items_newest_first=[],
        lookup_file=lambda _file_id: None,
    )

    assert breakdown.reported is None
    assert breakdown.failed_attempt is None


def test_an_overflow_reported_under_a_generic_code_keeps_its_attempt() -> None:
    """An in-process harness can surface "prompt is too long" as a plain runtime error."""
    labels = failed_turn_breakdown_labels(
        {
            "code": "RuntimeError",
            "message": "inner executor error: Prompt is too long: 210000 tokens > 200000",
            "context_breakdown": {"history_tokens": 205_000},
        }
    )

    assert json.loads(labels[FAILED_BREAKDOWN_LABEL_KEY]) == {"history_tokens": 205_000}
