"""Durable native-message decisions can be joined to their original web input."""

import json
import logging
from unittest.mock import Mock

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.runtime import pending_inputs, session_stream
from omnigent.server.routes._sessions.orchestration import _persist_external_conversation_item
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


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
    saved = {item.id: item for item in store.list_items(conv.id).data}
    assert len(saved) == 2
    assert saved[later_id].response_id == attrs["matched_response_id"]
    assert all(item.type != "error" for item in saved.values())
    assert "item_id" not in attrs and "error_item_id" not in attrs
    assert pending_inputs.snapshot_for(conv.id) == []
