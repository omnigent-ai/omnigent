"""Durable native-message decisions can be joined to their original web input."""

import json
import logging
from unittest.mock import Mock

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.runtime import pending_inputs, session_stream
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _record_native_input_lifecycle,
    _restore_unconfirmed_native_inputs,
    _settle_unconfirmed_native_input,
    _settle_undelivered_native_input,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.asyncio
async def test_rejected_durable_input_restores_by_client_id_after_server_restart(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "codex-native"})
    content = [{"type": "input_text", "text": "saved message"}]
    stable_id = "a" * 32
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                data=MessageData(role="user", content=content, user_authored=True),
                stable_id=stable_id,
                status="incomplete",
                created_by="alice@example.com",
            )
        ],
    )
    original_pending = pending_inputs.record(conv.id, content, stable_id=stable_id)
    original = pending_inputs.delivery_attributes_for(conv.id, original_pending)
    pending_inputs.reset_for_tests()
    newer = pending_inputs.record(conv.id, [{"type": "input_text", "text": "later message"}])

    for _ in range(2):
        await _settle_undelivered_native_input(
            store,
            conv.id,
            "resp_rejected",
            stable_id,
            pending_id=original_pending,
            delivery_attempt_id=str(original["delivery_attempt_id"]),
        )

    [saved] = store.list_items(conv.id).data
    assert saved.id == stable_id
    assert saved.response_id == "resp_rejected"
    assert saved.data.content == content
    assert saved.created_by == "alice@example.com"
    assert store.list_pending_inputs(conv.id) == []
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [newer]
    pending_inputs.resolve(conv.id, newer)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_append", [False, True])
async def test_skipped_message_logs_committed_ids_once_and_keeps_enqueue_identity(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_append: bool,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "claude-native-ui"})
    private = "private text from the missing message"
    first = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": private}],
        stable_id="a" * 32,
        background_titles_enabled=False,
    )
    pending_inputs.mark_delivery_stage(conv.id, first, "forward_accepted")
    before = pending_inputs.delivery_attributes_for(conv.id, first)
    second = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "later message"}],
        stable_id="b" * 32,
        background_titles_enabled=False,
    )
    body = SessionEventInput(
        type="external_conversation_item",
        data={
            "item_type": "message",
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "later message"}],
            },
            "response_id": "resp_native",
            "source_id": "native:later-record:0",
        },
    )
    published = Mock()
    monkeypatch.setattr(session_stream, "publish", published)
    caplog.set_level(logging.INFO)
    original_append = store.append
    if failed_append:
        monkeypatch.setattr(store, "append", Mock(side_effect=RuntimeError("store unavailable")))
        with pytest.raises(RuntimeError, match="store unavailable"):
            await _persist_external_conversation_item(conv.id, conv, body, store)
        assert not [
            r for r in caplog.records if getattr(r, "event_name", None) == "native_input_settled"
        ]
        assert [p["pending_id"] for p in pending_inputs.snapshot_for(conv.id)] == [first, second]
        monkeypatch.setattr(store, "append", original_append)

    matched_id = await _persist_external_conversation_item(conv.id, conv, body, store)
    rows = [
        record_to_row(r, "server")
        for r in caplog.records
        if getattr(r, "event_name", None) == "native_input_settled"
    ]
    assert len(rows) == 2
    lost, matched = [row["attributes"] for row in rows]
    saved = {item.id: item for item in store.list_items(conv.id).data}
    assert lost["outcome"] == "skipped_without_native_record"
    assert lost["pending_id"] == first
    assert lost["input_stable_id"] == "a" * 32
    assert lost["delivery_attempt_id"] == before["delivery_attempt_id"]
    assert lost["input_enqueued_at_ms"] == str(before["input_enqueued_at_ms"])
    assert lost["last_delivery_stage"] == "forward_accepted"
    assert lost["cause_class"] == "unknown"
    assert lost["reason_code"] == "no_identity_linked_lifecycle"
    assert int(lost["pending_age_ms"]) >= 0
    assert saved[lost["error_item_id"]].data.code == "native_prompt_not_recorded"
    assert saved[lost["error_item_id"]].response_id == lost["response_id"]
    assert lost["matched_item_id"] == matched_id
    assert lost["matched_response_id"] == saved[matched_id].response_id == "resp_native"
    assert lost["matched_pending_id"] == second
    assert lost["match_method"] == "normalized_text"
    assert matched["pending_id"] == second
    assert matched["input_stable_id"] == "b" * 32
    assert matched["outcome"] == "native_transcript_matched"
    assert matched["item_id"] == matched_id
    assert private not in json.dumps(rows)
    receipts = [
        session_stream._sse_safe_attributes(call.args[1]) for call in published.call_args_list
    ]
    assert {
        r["item_id"]: r["cleared_pending_id"] for r in receipts if "cleared_pending_id" in r
    } == {
        lost["item_id"]: first,
        matched_id: second,
    }
    assert any(
        r.get("item_id") == lost["error_item_id"] and r.get("response_id") == lost["response_id"]
        for r in receipts
    )

    newer = pending_inputs.record(conv.id, [{"type": "input_text", "text": "later message"}])
    assert await _persist_external_conversation_item(conv.id, conv, body, store) == matched_id
    assert (
        len(
            [r for r in caplog.records if getattr(r, "event_name", None) == "native_input_settled"]
        )
        == 2
    )
    assert [p["pending_id"] for p in pending_inputs.snapshot_for(conv.id)] == [newer]
    pending_inputs.resolve(conv.id, newer)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_client_id", [False, True])
@pytest.mark.parametrize(
    ("outcome", "cause_class", "reason_code"),
    [
        ("completed", "accepted_missing_native_record", "completed_without_native_record"),
        ("cancelled", "interrupted_or_lifecycle", "response_cancelled"),
        ("incomplete", "interrupted_or_lifecycle", "response_incomplete"),
    ],
)
async def test_lifecycle_without_a_native_record_has_a_stable_cause_class(
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    cause_class: str,
    reason_code: str,
    with_client_id: bool,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "codex-native"})
    first = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "missing"}],
        stable_id="a" * 32 if with_client_id else None,
        background_titles_enabled=False,
    )
    second = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "later"}],
        stable_id="b" * 32,
        background_titles_enabled=False,
    )
    identity = {
        key: value
        for key, value in pending_inputs.delivery_attributes_for(conv.id, first).items()
        if key in {"pending_id", "delivery_attempt_id", "input_stable_id"}
    }
    _record_native_input_lifecycle(
        conv.id,
        {
            "type": "response.in_progress",
            **identity,
            "response": {"id": "resp_missing"},
        },
    )
    _record_native_input_lifecycle(
        conv.id,
        {
            "type": f"response.{outcome}",
            **identity,
            "response": {"id": "resp_missing"},
        },
    )
    assert pending_inputs.has_pending(conv.id)
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [
        first,
        second,
    ]
    caplog.set_level(logging.INFO)

    await _persist_external_conversation_item(
        conv.id,
        conv,
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "later"}],
                },
                "response_id": "resp_later",
                "source_id": "codex:later-record:0",
            },
        ),
        store,
    )

    [record] = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "native_input_settled"
        and record.attributes.get("pending_id") == first
    ]
    assert record.attributes["outcome"] == "skipped_without_native_record"
    assert record.attributes["cause_class"] == cause_class
    assert record.attributes["reason_code"] == reason_code
    assert record.attributes["lifecycle_response_id"] == "resp_missing"
    saved = {item.id: item for item in store.list_items(conv.id).data}
    assert saved[record.attributes["item_id"]].data.content[0]["text"] == "missing"
    assert saved[record.attributes["error_item_id"]].data.code == "native_prompt_not_recorded"


@pytest.mark.parametrize("invalid_key", ["input_stable_id", "pending_id", "delivery_attempt_id"])
def test_malformed_lifecycle_identity_does_not_fall_back_to_another_identifier(
    invalid_key: str,
) -> None:
    session_id = "conv_malformed_lifecycle_identity"
    pending_id = pending_inputs.record(session_id, [], stable_id="a" * 32)
    original = pending_inputs.delivery_attributes_for(session_id, pending_id)
    try:
        _record_native_input_lifecycle(
            session_id,
            {**original, "type": "response.completed", invalid_key: "invalid identity"},
        )
        assert (
            pending_inputs.delivery_attributes_for(session_id, pending_id)["last_delivery_stage"]
            == "server_queued"
        )
    finally:
        pending_inputs.resolve(session_id, pending_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_uncertain_or_interrupted_input_does_not_report_missing_message(
    db_uri: str, caplog: pytest.LogCaptureFixture, interrupted: bool
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "claude-native-ui"})
    first = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "original"}],
        stable_id="a" * 32,
        background_titles_enabled=False,
    )
    uncertain = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "uncertain"}],
        stable_id="b" * 32,
        background_titles_enabled=False,
    )
    last = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "last"}],
        stable_id="c" * 32,
        background_titles_enabled=False,
    )
    original = pending_inputs.delivery_attributes_for(conv.id, uncertain)
    if interrupted:
        _record_native_input_lifecycle(
            conv.id,
            {"type": "response.cancelled", "input_stable_id": "b" * 32},
        )
        pending_inputs.mark_interrupted(conv.id, [uncertain])
    caplog.set_level(logging.INFO)
    await _persist_external_conversation_item(
        conv.id,
        conv,
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "original" if interrupted else "reformatted",
                        }
                    ],
                },
                "source_id": "native:reformatted:0",
            },
        ),
        store,
    )
    [record] = [
        r for r in caplog.records if getattr(r, "event_name", None) == "native_input_settled"
    ]
    assert record.attributes["outcome"] == (
        "native_transcript_matched" if interrupted else "native_transcript_fifo_attributed"
    )
    assert record.attributes["pending_id"] == first
    assert record.attributes["match_method"] == (
        "normalized_text" if interrupted else "fifo_fallback"
    )
    assert "error_item_id" not in record.attributes
    later_id = await _persist_external_conversation_item(
        conv.id,
        conv,
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {"role": "user", "content": [{"type": "input_text", "text": "last"}]},
                "source_id": "native:last:0",
                "response_id": "resp_later",
            },
        ),
        store,
    )
    expected_outcome = "user_interrupted" if interrupted else "prior_fifo_match_uncertain"
    [uncertain_record] = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "native_input_settled"
        and r.attributes["outcome"] == expected_outcome
    ]
    attrs = uncertain_record.attributes
    assert attrs["pending_id"] == uncertain
    assert attrs["input_stable_id"] == "b" * 32
    assert attrs["delivery_attempt_id"] == original["delivery_attempt_id"]
    assert attrs["input_enqueued_at_ms"] == original["input_enqueued_at_ms"]
    assert attrs["matched_item_id"] == later_id
    assert attrs["matched_pending_id"] == last
    assert attrs["matched_response_id"] == "resp_later"
    assert attrs["match_method"] == "normalized_text"
    assert attrs["cause_class"] == (
        "interrupted_or_lifecycle" if interrupted else "reconciliation_ambiguity"
    )
    assert attrs["reason_code"] == (
        "response_cancelled" if interrupted else "prior_fifo_attribution"
    )
    saved = {item.id: item for item in store.list_items(conv.id).data}
    assert len(saved) == 2
    assert saved[later_id].response_id == attrs["matched_response_id"]
    assert all(item.type != "error" for item in saved.values())
    assert "item_id" not in attrs and "error_item_id" not in attrs
    assert pending_inputs.snapshot_for(conv.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_append", [False, True])
@pytest.mark.parametrize("same_text", [False, True])
async def test_uncertain_input_retains_authored_content_and_cannot_be_replayed(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    fail_append: bool,
    same_text: bool,
) -> None:
    from omnigent.stores.conversation_store import UNCONFIRMED_NATIVE_INPUT_RESPONSE_ID

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "claude-native-ui"})
    content = [
        {"type": "input_file", "file_id": "original-file", "filename": "notes.txt"},
        {"type": "input_text", "text": "keep the exact authored message"},
    ]
    pending_id = pending_inputs.record(
        conv.id,
        content,
        stable_id="c" * 32,
        created_by="alice@example.com",
    )
    identity = pending_inputs.delivery_attributes_for(conv.id, pending_id)
    event = {"type": "response.failed", **identity}
    published = Mock()
    monkeypatch.setattr(session_stream, "publish", published)
    append = store.append
    if fail_append:
        monkeypatch.setattr(store, "append", Mock(side_effect=RuntimeError("store unavailable")))
        with pytest.raises(RuntimeError, match="store unavailable"):
            await _settle_unconfirmed_native_input(store, conv.id, event)
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [
            pending_id
        ]
        assert store.list_items(conv.id).data == []
        monkeypatch.setattr(store, "append", append)
    await _settle_unconfirmed_native_input(store, conv.id, event)
    await _settle_unconfirmed_native_input(store, conv.id, event)
    [saved] = store.list_items(conv.id).data
    assert saved.id == "c" * 32
    assert saved.status == "incomplete"
    assert saved.response_id == UNCONFIRMED_NATIVE_INPUT_RESPONSE_ID
    assert saved.data.content == content
    assert saved.created_by == "alice@example.com"
    assert store.list_pending_inputs(conv.id) == []
    assert pending_inputs.snapshot_for(conv.id) == []
    assert not pending_inputs.has_pending(conv.id)
    [saved_event] = [
        call.args[1]
        for call in published.call_args_list
        if call.args[1]["type"] == "session.input.consumed"
    ]
    assert saved_event["data"]["delivery_outcome"] == "delivery_unconfirmed"
    assert pending_inputs.resolve_oldest(conv.id) is None

    pending_inputs.reset_for_tests()
    store = SqlAlchemyConversationStore(db_uri)
    later = pending_inputs.record(
        conv.id,
        [
            {
                "type": "input_text",
                "text": "keep the exact authored message" if same_text else "newer input",
            }
        ],
        stable_id="d" * 32,
    )
    await _restore_unconfirmed_native_inputs(store, conv.id)
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [later]
    mirror_id = await _persist_external_conversation_item(
        conv.id,
        conv,
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "keep the exact authored message"}],
                },
                "source_id": "native:independent-user-record",
                "response_id": "resp_native",
            },
        ),
        store,
    )
    assert mirror_id != saved.id
    still_uncertain = store.get_item(conv.id, saved.id)
    assert still_uncertain is not None
    assert still_uncertain.response_id == UNCONFIRMED_NATIVE_INPUT_RESPONSE_ID
    assert still_uncertain.data.content == content
    assert still_uncertain.created_by == "alice@example.com"
    [native] = [item for item in store.list_items(conv.id).data if item.id == mirror_id]
    assert native.created_by is None
    assert native.data.content == [
        {"type": "input_text", "text": "keep the exact authored message"}
    ]
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [later]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["pending_id", "delivery_attempt_id", "input_stable_id", "missing_attempt", "malformed"],
)
async def test_uncertain_disposition_requires_consistent_original_input_identity(
    db_uri: str,
    mismatch: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test")
    pending_id = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "do not attribute a stale failure"}],
        stable_id="e" * 32,
    )
    event = dict(pending_inputs.delivery_attributes_for(conv.id, pending_id))
    if mismatch == "missing_attempt":
        del event["delivery_attempt_id"]
    elif mismatch == "malformed":
        event["input_stable_id"] = "invalid"
    else:
        event[mismatch] = "pending_" + "f" * 32 if mismatch == "pending_id" else "f" * 32
    await _settle_unconfirmed_native_input(store, conv.id, event)
    assert store.list_items(conv.id).data == []
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [pending_id]
