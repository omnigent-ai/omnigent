"""User messages tests for Codex forwarder."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.main import _codex_rollout_records_from_session_items
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
    _seed_active_turn,
)


@pytest.mark.parametrize(
    "content,expected",
    [
        ([{"type": "image", "url": "data:image/png;base64,AAAA"}], True),
        ([{"type": "input_file", "file_data": "data:application/pdf;base64,AAAA"}], True),
        ([{"type": "text", "text": "hi"}, {"type": "image", "url": "data:x"}], True),
        ([{"type": "text", "text": "only text"}], False),
        ([], False),
        ("not a list", False),
    ],
)
def test_user_message_has_file_content(content: object, expected: bool) -> None:
    """
    Detect a non-text (image/file) block in a Codex ``userMessage``.

    Drives the gate that decides whether a text-less ``userMessage`` is a
    real image-bearing message that must be persisted. ``True`` for any
    block whose ``type`` is not ``"text"``, else ``False``. A wrong result
    re-opens the image-only regression (text-less image skipped → dropped
    bubble + pending-FIFO bleed) or makes text-only messages post twice.
    """
    assert fwd._user_message_has_file_content({"content": content}) is expected


@pytest.mark.asyncio
async def test_post_user_message_image_only_posts_empty_content() -> None:
    """
    An image-only ``userMessage`` is posted with EMPTY Omnigent content.

    Regression guard for the image-only bleed/ordering bug: the forwarder
    must post the user item (so the server drains the pending-input FIFO
    entry and folds the image in by file_id). The posted content is empty
    — the base64 ``data:`` URL Codex echoes must NOT be written into text.
    A bail here would drop the user bubble and leak the pending entry into
    the next message.
    """
    client = _RecordingClient()
    item = {
        "type": "userMessage",
        "content": [{"type": "image", "url": "data:image/png;base64,AAAA"}],
    }

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1, "image-only userMessage must still be posted"
    _url, body = client.posts[0]
    item_data = body["data"]["item_data"]
    assert item_data["role"] == "user"
    # Empty content: the image is supplied server-side from the pending
    # entry; echoing Codex's base64 url here would re-introduce the freeze.
    assert item_data["content"] == []


@pytest.mark.asyncio
async def test_post_user_message_text_posts_input_text() -> None:
    """A text ``userMessage`` posts an ``input_text`` block (unchanged path)."""
    client = _RecordingClient()
    item = {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["data"]["item_data"]["content"] == [{"type": "input_text", "text": "hello"}]


@pytest.mark.asyncio
async def test_post_user_message_truly_empty_is_skipped() -> None:
    """
    A ``userMessage`` with neither text nor a file block is not posted.

    Without this guard the forwarder would emit spurious empty user
    bubbles. A failure (a post recorded) means the empty-skip branch broke.
    """
    client = _RecordingClient()
    item = {"type": "userMessage", "content": []}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert client.posts == []


@pytest.mark.parametrize(
    ("echoing_replay", "copies", "expected_duplicates"),
    [
        (True, ["resumed", "live"], [False, True]),
        (True, ["live", "resumed"], [False, True]),
        (True, ["resumed", "resumed", "live", "live"], [False, False, True, True]),
        (True, ["resumed", "live", "live"], [False, True, False]),
        (False, ["resumed", "live"], [False, False]),
        (False, ["live", "resumed", "resumed"], [False, True, False]),
    ],
    ids=[
        "replay_then_echo",
        "echo_then_replay",
        "repeated_input",
        "identical_steer_after_echo",
        "late_join_then_identical_steer",
        "live_then_late_join_replay",
    ],
)
def test_pair_user_message_copy_matches_each_resumed_input_once(
    echoing_replay: bool, copies: list[str], expected_duplicates: list[bool]
) -> None:
    """Each resumed copy pairs with at most one live copy of the same input."""
    state = fwd._CodexForwarderState(parent_session_id="conv_123")
    if echoing_replay:
        state.resume_echo_thread_ids.add("thread_123")
    key = ("thread_123", "turn_123", '[{"text": "Run the tests again.", "type": "text"}]')

    duplicates = [state.pair_user_message_copy(key, resumed=copy == "resumed") for copy in copies]

    assert duplicates == expected_duplicates


def test_user_message_copy_balance_keeps_unmatched_copies_until_turn_ends() -> None:
    """Paired copies and a finished turn's copies are not retained."""
    state = fwd._CodexForwarderState(parent_session_id="conv_123")
    state.resume_echo_thread_ids.add("thread_123")
    paired = ("thread_123", "turn_123", "digest_paired")
    unmatched = ("thread_123", "turn_123", "digest_unmatched")
    other_turn = ("thread_123", "turn_456", "digest_unmatched")

    state.pair_user_message_copy(paired, resumed=True)
    state.pair_user_message_copy(paired, resumed=False)
    state.pair_user_message_copy(unmatched, resumed=False)
    state.pair_user_message_copy(other_turn, resumed=False)
    assert state.user_message_copy_balance == {unmatched: 1, other_turn: 1}

    state.forget_user_message_copies("turn_123")
    assert state.user_message_copy_balance == {other_turn: 1}


def _skill_user(item_id: str, client_id: str, text: str = "$example  keep\nthis") -> dict:
    return {
        "type": "userMessage",
        "id": item_id,
        "clientId": client_id,
        "content": [
            {"type": "text", "text": text, "text_elements": []},
            {"type": "skill", "name": "example", "path": "/native/example/SKILL.md"},
        ],
    }


def _rollout_user(item: dict) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": "turn_1",
            "item": {"type": "UserMessage", "id": item["id"], "content": item["content"]},
        },
    }


def _rollout_skill(
    item_id: str, text: str, *, kind: str = "skills.selected_skill_instructions"
) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": item_id,
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "turn_1",
                "content_item_kinds": [kind],
            },
        },
    }


@pytest.mark.asyncio
async def test_skill_echoes_and_native_expansions_keep_same_turn_input_order(
    tmp_path: Path,
) -> None:
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_1")
    rollout = tmp_path / "codex-home" / "sessions" / "rollout-thread_123.jsonl"
    rollout.parent.mkdir(parents=True)
    first = _skill_user("live-1", "omnigent-skill:first")
    second = _skill_user("live-2", "omnigent-skill:second")
    tui = _skill_user("live-3", "ordinary-codex-id", "$example directly")
    rollout.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                _rollout_user(first),
                _rollout_skill("forged", "<skill>ordinary user text</skill>", kind="user.text"),
                _rollout_skill("expansion-1", "<skill>original body</skill>\n"),
                _rollout_user(second),
                _rollout_skill("expansion-2", "<skill>changed body</skill>\n"),
                _rollout_user(tui),
                _rollout_skill("expansion-3", "<skill>native terminal body</skill>"),
            ]
        )
    )

    async def complete(item: dict) -> None:
        await fwd._handle_completed_item(
            client,
            "conv_x",
            {"threadId": "thread_123", "turnId": "turn_1", "item": item},
            bridge_dir=tmp_path,
            forwarder_state=state,
        )

    await complete(first)
    await complete({"type": "agentMessage", "id": "model-1", "text": "First response"})
    await complete({**first, "id": "item-1"})
    await complete(second)
    await complete({**second, "id": "item-3"})
    await complete(tui)
    await complete({"type": "agentMessage", "id": "model-2", "text": "Next response"})

    events = [body["data"] for _, body in client.posts]
    assert [event["source_id"].removeprefix("thread_123:turn_1:") for event in events] == [
        "omnigent-skill:first",
        "expansion-1",
        "model-1",
        "omnigent-skill:second",
        "expansion-2",
        "live-3",
        "expansion-3",
        "model-2",
    ]
    assert all(event["response_id"] == "codex_turn_1" for event in events)
    for event in (events[0], events[3]):
        assert event["item_type"] == "slash_command"
        assert event["item_data"] == {
            "kind": "skill",
            "agent": "codex-native-ui",
            "name": "example",
            "arguments": " keep\nthis",
            "native_invocation": "$example  keep\nthis",
        }
    assert events[1]["item_data"] == {
        "role": "user",
        "is_meta": True,
        "content": [{"type": "input_text", "text": "<skill>original body</skill>\n"}],
    }
    assert events[4]["item_data"]["content"][0]["text"] == "<skill>changed body</skill>\n"
    assert events[5]["item_data"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "$example directly"}],
    }
    assert events[6]["item_data"] == {
        "role": "user",
        "is_meta": True,
        "content": [{"type": "input_text", "text": "<skill>native terminal body</skill>"}],
    }


@pytest.mark.parametrize(
    ("legacy", "recoverable"),
    [(False, True), (True, True), (False, False)],
    ids=["modern", "legacy_late_echo", "incomplete_snapshot"],
)
@pytest.mark.asyncio
async def test_backfill_keeps_missed_skill_before_same_turn_steer(
    tmp_path: Path, legacy: bool, recoverable: bool
) -> None:
    client = _RecordingClient()
    _seed_active_turn(tmp_path, "turn_1")
    first = _skill_user("live-1", "omnigent-skill:first")
    second = _skill_user("live-2", "omnigent-skill:second")
    assistant = {"type": "agentMessage", "id": "model-1", "text": "Reply"}
    rollout = tmp_path / "codex-home" / "sessions" / "rollout-thread_123.jsonl"
    rollout.parent.mkdir(parents=True)

    def rollout_user(item: dict) -> dict:
        if not legacy:
            return _rollout_user(item)
        return {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "client_id": item["clientId"],
                "message": item["content"][0]["text"],
            },
        }

    first_rows = [
        rollout_user(first),
        _rollout_skill("expansion-1", "<skill>first body</skill>"),
    ]
    second_rows = [
        rollout_user(second),
        _rollout_skill("expansion-2", "<skill>second body</skill>"),
    ]
    if legacy:
        first_rows.insert(
            0, {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn_1"}}
        )
    rollout.write_text(
        "".join(json.dumps(row) + "\n" for row in first_rows + ([] if legacy else second_rows))
    )

    snapshot_users = [first, second]
    if legacy:
        snapshot_users = [
            {**item, "id": f"item-{index}", "content": item["content"][:1]}
            for index, item in enumerate(snapshot_users, start=1)
        ]
    snapshot = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [{"id": "turn_1", "items": [*snapshot_users, assistant]}],
            }
        }
    }
    native_client = MagicMock()
    native_client.request = AsyncMock(return_value=snapshot if recoverable else {})
    state = fwd._CodexForwarderState(codex_client=native_client)

    async def complete(item: dict) -> None:
        await fwd._handle_completed_item(
            client,
            "conv_x",
            {"threadId": "thread_123", "turnId": "turn_1", "item": item},
            bridge_dir=tmp_path,
            forwarder_state=state,
        )

    await complete(second)
    if legacy:
        with rollout.open("a") as stream:
            for row in second_rows:
                stream.write(json.dumps(row) + "\n")
    await complete(assistant)
    if not recoverable:
        assert [
            body["data"]["source_id"].removeprefix("thread_123:turn_1:")
            for _, body in client.posts
            if body["type"] == "external_conversation_item"
        ] == ["omnigent-skill:second", "model-1"]
        native_client.request.return_value = snapshot
    tracker = fwd._CodexElicitationTaskTracker()
    try:
        for _ in range(2):
            await fwd._replay_resume_response(
                client,
                session_id="conv_x",
                bridge_dir=tmp_path,
                response=snapshot,
                usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),
                elicitation_tracker=tracker,
                forwarder_state=state,
            )
    finally:
        await tracker.close()

    events = [
        body["data"] for _, body in client.posts if body["type"] == "external_conversation_item"
    ]
    sources = [event["source_id"].removeprefix("thread_123:turn_1:") for event in events]
    if not recoverable:
        assert sources == ["omnigent-skill:second", "model-1", "omnigent-skill:first"]
        return
    assert sources == [
        "omnigent-skill:first",
        "expansion-1",
        "omnigent-skill:second",
        "expansion-2",
        "model-1",
    ]
    records = _codex_rollout_records_from_session_items(
        [
            {
                "id": event["source_id"],
                "type": event["item_type"],
                "response_id": event["response_id"],
                "model": event["item_data"].get("agent"),
                **event["item_data"],
            }
            for event in events
        ],
        session_id="conv_rebuilt",
        external_session_id="rebuilt-thread",
        cwd=tmp_path,
        model_provider="local",
        cli_version="0.167.0",
    )
    assert [
        (record["payload"]["role"], record["payload"]["content"][0]["text"])
        for record in records
        if record["type"] == "response_item"
    ] == [
        ("user", "$example  keep\nthis"),
        ("user", "<skill>first body</skill>"),
        ("user", "$example  keep\nthis"),
        ("user", "<skill>second body</skill>"),
        ("assistant", "Reply"),
    ]


@pytest.mark.asyncio
async def test_terminal_catches_up_a_late_skill_expansion_and_missed_input(tmp_path: Path) -> None:
    client = _RecordingClient()
    _seed_active_turn(tmp_path, "turn_1")
    rollout = tmp_path / "codex-home" / "sessions" / "rollout-thread_123.jsonl"
    rollout.parent.mkdir(parents=True)
    user = _skill_user("item-5", "omnigent-skill:first", "$example")
    user["content"].pop()
    expansion = _rollout_skill("expansion-1", "<skill>the native body</skill>")
    rollout.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn_1"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "client_id": "omnigent-skill:first",
                        "message": "$example",
                    },
                },
                expansion,
            )
        )
    )
    native_client = MagicMock()
    native_client.request = AsyncMock(
        return_value={"result": {"thread": {"turns": [{"id": "turn_1", "items": [user]}]}}},
    )
    state = fwd._CodexForwarderState(codex_client=native_client)
    await fwd._handle_completed_item(
        client,
        "conv_x",
        {
            "threadId": "thread_123",
            "turnId": "turn_1",
            "item": {"type": "reasoning", "id": "partial"},
        },
        bridge_dir=tmp_path,
        forwarder_state=state,
    )
    assert client.posts == []
    with rollout.open("a") as stream:
        stream.write("\n")
    tracker = fwd._CodexElicitationTaskTracker()
    try:
        await fwd._handle_terminal_turn_boundary(
            client,
            session_id="conv_x",
            bridge_dir=tmp_path,
            method="turn/completed",
            params={"threadId": "thread_123", "turn": {"id": "turn_1", "status": "interrupted"}},
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),
            delta_coalescer=None,
            elicitation_tracker=tracker,
            codex_client=None,
            forwarder_state=state,
        )
    finally:
        await tracker.close()
    native_client.request.assert_awaited_once_with("thread/resume", {"threadId": "thread_123"})
    items = [
        body["data"] for _, body in client.posts if body["type"] == "external_conversation_item"
    ]
    assert [event["item_type"] for event in items] == ["slash_command", "message"]
    assert items[0]["item_data"]["arguments"] == ""
    assert items[1]["item_data"]["content"] == expansion["payload"]["content"]


@pytest.mark.asyncio
async def test_rebuilt_skill_echo_does_not_append_an_existing_command_card() -> None:
    client = _RecordingClient()
    await fwd._post_user_message(
        client,
        "conv_x",
        {"threadId": "thread_123", "turnId": "rollout-2"},
        {
            "type": "userMessage",
            "id": "item-1",
            "content": [
                {
                    "type": "text",
                    "text": "$example again",
                    "text_elements": [
                        {
                            "byteRange": {"start": 0, "end": 0},
                            "placeholder": "omnigent-skill-history:existing-slash-id",
                        }
                    ],
                }
            ],
        },
    )
    assert client.posts == []
