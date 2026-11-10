"""Durable native-message decisions can be joined to their original web input."""

import base64
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.harnesses.codex_native import forwarder as codex_forwarder
from omnigent.inner.codex_native_executor import _content_to_input_items
from omnigent.runtime import pending_inputs, session_stream
from omnigent.server.routes._sessions.orchestration import (
    _persist_external_conversation_item,
    _record_native_input_lifecycle,
    _settle_undelivered_native_input,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.harnesses.codex_native.forwarder._support import _RecordingClient


@pytest.mark.asyncio
@pytest.mark.parametrize("with_prompt", [False, True], ids=["file-only", "prompt-first"])
@pytest.mark.parametrize("failed_append", [False, True])
async def test_spilled_text_file_receipt_keeps_earlier_inputs_and_correct_upload(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_prompt: bool,
    failed_append: bool,
) -> None:
    """Convert, mirror and persist a large upload using its exact native receipt."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "codex-native-ui"})
    first = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "earlier"}, {"type": "input_file", "file_id": "file_old"}],
    )
    file_block = {"type": "input_file", "filename": "large.txt", "file_id": "file_current"}
    prompt = [{"type": "input_text", "text": "compare this upload"}] if with_prompt else []
    current_content = [*prompt, file_block]
    current = pending_inputs.record(conv.id, current_content, created_by="alice@example.com")
    attempt = pending_inputs.delivery_attributes_for(conv.id, current)["delivery_attempt_id"]
    data_uri = "data:text/plain;base64," + base64.b64encode(b"x" * 1_100_000).decode()
    native_content = _content_to_input_items(
        [*prompt, {**file_block, "file_data": data_uri}], tmp_path
    )
    client = _RecordingClient()
    await codex_forwarder._post_user_message(
        client,
        conv.id,
        {"turnId": "native-turn"},
        {
            "id": "native-record",
            "type": "userMessage",
            "clientId": attempt,
            "content": native_content,
        },
    )
    [(_url, envelope)] = client.posts
    body = SessionEventInput.model_validate(envelope)
    assert body.data["delivery_attempt_id"] == attempt
    published = Mock()
    monkeypatch.setattr(session_stream, "publish", published)
    original_append = store.append
    if failed_append:
        monkeypatch.setattr(store, "append", Mock(side_effect=RuntimeError("store unavailable")))
        with pytest.raises(RuntimeError, match="store unavailable"):
            await _persist_external_conversation_item(conv.id, conv, body, store)
        assert pending_inputs.pending_ids(conv.id) == [first, current]
        monkeypatch.setattr(store, "append", original_append)

    item_id = await _persist_external_conversation_item(conv.id, conv, body, store)
    [saved] = store.list_items(conv.id).data
    assert saved.id == item_id
    assert saved.data.content[0] == file_block
    assert saved.created_by == "alice@example.com"
    assert pending_inputs.pending_ids(conv.id) == [first]
    receipts = [
        session_stream._sse_safe_attributes(call.args[1]) for call in published.call_args_list
    ]
    assert [r["cleared_pending_id"] for r in receipts if "cleared_pending_id" in r] == [current]

    newer = pending_inputs.record(conv.id, current_content)
    assert await _persist_external_conversation_item(conv.id, conv, body, store) == item_id
    assert len(store.list_items(conv.id).data) == 1
    assert pending_inputs.pending_ids(conv.id) == [first, newer]
    pending_inputs.resolve(conv.id, first)
    pending_inputs.resolve(conv.id, newer)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["absent", "unknown", "different-session", "invalid", None])
async def test_native_receipt_miss_does_not_consume_a_matching_pending_message(
    db_uri: str, receipt: str | None
) -> None:
    """Only a missing legacy receipt permits the existing text-matching fallback."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "codex-native-ui"})
    content = [{"type": "input_text", "text": "identical message"}]
    pending = pending_inputs.record(conv.id, content)
    other_session = f"other-{conv.id}"
    other = pending_inputs.record(other_session, content)
    values = {
        "unknown": "f" * 32,
        "different-session": pending_inputs.delivery_attributes_for(other_session, other)[
            "delivery_attempt_id"
        ],
        "invalid": "not-a-server-generated-id",
        None: None,
    }
    data = {
        "item_type": "message",
        "item_data": {"role": "user", "content": content},
        "response_id": "resp_native",
        "source_id": "native-record",
    }
    if receipt != "absent":
        data["delivery_attempt_id"] = values[receipt]
    await _persist_external_conversation_item(
        conv.id, conv, SessionEventInput(type="external_conversation_item", data=data), store
    )
    assert pending_inputs.pending_ids(conv.id) == ([] if receipt == "absent" else [pending])
    assert pending_inputs.pending_ids(other_session) == [other]
    pending_inputs.resolve(conv.id, pending)
    pending_inputs.resolve(other_session, other)


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
