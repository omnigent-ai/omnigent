"""Pending startup messages reuse item status, ordering, encryption and deletion."""

import base64
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from omnigent.db.db_models import SqlConversationItem, workspace_scope
from omnigent.entities.conversation import MessageData, NewConversationItem, StartupInputOptions
from omnigent.errors import OmnigentError
from omnigent.stores.conversation_store import MAX_PENDING_INPUTS, PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _input(message="saved prompt", **kwargs):
    return NewConversationItem(
        type="message",
        response_id=PENDING_INPUT_RESPONSE_ID,
        status="in_progress",
        stable_id=uuid4().hex,
        data=MessageData(role="user", content=[{"type": "input_text", "text": message}]),
        **kwargs,
    )


def test_restart_and_concurrent_submission_claims(db_uri):
    first = SqlAlchemyConversationStore(db_uri)
    second = SqlAlchemyConversationStore(db_uri)
    session = first.create_conversation()
    item = first.append(session.id, [_input()])[0]
    assert second.list_pending_inputs(session.id) == [item]
    assert second.list_items(session.id).data == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda store: store.transition_pending_input(
                    session.id, item.id, expected="in_progress", status="incomplete"
                ),
                [first, second],
            )
        )
    assert sum(claims) == 1
    assert second.list_pending_inputs(session.id)[0].status == "incomplete"


def test_native_receipt_replaces_pending_row_once_and_keeps_order(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation()
    first, second = store.append(session.id, [_input("first"), _input("second")])
    native = NewConversationItem(
        type="message",
        response_id="native-turn",
        stable_id=uuid4().hex,
        replaces_pending_id=first.id,
        data=first.data,
    )
    committed = store.append(session.id, [native])[0]
    assert committed.created_at == first.created_at
    assert store.get_item(session.id, first.id) is None
    assert store.list_pending_inputs(session.id) == [second]
    # A mirror retry must neither delete the next input nor append another item.
    assert store.append(session.id, [native])[0].deduplicated
    assert store.list_pending_inputs(session.id) == [second]
    assert store.list_items(session.id).data == [committed]
    with store._conv_session("test_input_positions") as db:
        assert list(
            db.scalars(select(SqlConversationItem.id).order_by(SqlConversationItem.position))
        ) == [committed.id, second.id]


def test_sdk_activation_keeps_stable_id_and_becomes_history(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation()
    saved = store.append(session.id, [_input()])[0]
    item = NewConversationItem(
        type="message",
        response_id="sdk-turn",
        stable_id=saved.id,
        replaces_pending_id=saved.id,
        data=saved.data,
    )
    committed = store.append(session.id, [item])[0]
    assert committed.id == saved.id and not committed.deduplicated
    assert store.list_pending_inputs(session.id) == []
    assert store.list_items(session.id).data == [committed]
    assert store.append(session.id, [item])[0].deduplicated


def test_bound_pending_read_and_failed_claim_does_not_change_history(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation()
    saved = store.append(session.id, [_input() for _ in range(MAX_PENDING_INPUTS)])
    with pytest.raises(OmnigentError, match="Too many messages"):
        store.append(session.id, [_input()])
    assert not store.transition_pending_input(
        session.id, saved[0].id, expected="failed", status="completed"
    )
    assert len(store.list_pending_inputs(session.id)) == MAX_PENDING_INPUTS
    assert store.list_items(session.id).data == []
    if not db_uri.startswith("sqlite"):
        return  # The plan check below uses SQLite's EXPLAIN syntax.
    with store._conv_session("test_pending_query_plan") as db:
        plan = db.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM conversation_items "
                "WHERE workspace_id=0 AND conversation_id=:id AND response_id=:response LIMIT 100"
            ),
            {"id": bytes.fromhex(session.id), "response": PENDING_INPUT_RESPONSE_ID},
        ).all()
    assert "ix_conversation_items_response_id" in str(plan)


def test_host_pagination_and_workspace_isolation(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    host_id = uuid4().hex
    ids = sorted(store.create_conversation().id for _ in range(3))
    for session_id in ids:
        store.set_host_id(session_id, host_id, "/tmp/workspace")
        store.append(session_id, [_input()])
    assert store.list_host_conversation_ids(host_id, limit=2) == ids[:2]
    assert store.list_host_conversation_ids(host_id, after=ids[1], limit=2) == ids[2:]
    with workspace_scope(17):
        assert store.list_host_conversation_ids(host_id) == []
        assert store.list_pending_inputs(ids[0]) == []
    if not db_uri.startswith("sqlite"):
        return  # The plan check below uses SQLite's EXPLAIN syntax.
    with store._session("test_host_query_plan") as db:
        plan = db.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM omnigent_conversation_metadata "
                "WHERE workspace_id=0 AND host_id=:host AND id>:after ORDER BY id LIMIT 2"
            ),
            {"host": bytes.fromhex(host_id), "after": bytes.fromhex(ids[0])},
        ).all()
    assert "ix_conversation_metadata_host_id" in str(plan)


@pytest.mark.asyncio
async def test_deleting_conversation_deletes_pending_input(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation()
    store.append(session.id, [_input()])
    assert await store.delete_conversation(session.id)
    assert store.list_pending_inputs(session.id) == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_fork_does_not_copy_undelivered_messages(db_uri, cancelled):
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation()
    store.set_labels(session.id, {"omnigent.startup": "pending"})
    [saved] = store.append(session.id, [_input()])
    if cancelled:
        assert store.transition_pending_input(
            session.id,
            saved.id,
            expected="in_progress",
            status=None,
        )
        assert store.get_item(session.id, saved.id) is None
    assert store.list_items(session.id).data == []
    fork = store.fork_conversation(session.id)
    assert store.list_pending_inputs(fork.id) == []
    assert store.list_items(fork.id).data == []
    assert "omnigent.startup" not in fork.labels


def test_pending_payload_and_options_use_item_encoding_hooks(db_uri):
    class EncodedStore(SqlAlchemyConversationStore):
        def _encode_item_data(self, data_json: str) -> str:
            return base64.b64encode(data_json.encode()).decode("ascii")

        def _decode_item_data_batch(self, stored: list[str]) -> list[str]:
            return [base64.b64decode(value).decode() for value in stored]

    store = EncodedStore(db_uri)
    session = store.create_conversation()
    item = _input("private startup instruction")
    item.data.startup_options = StartupInputOptions(model_override="chosen-model", tools=[])
    [saved] = store.append(session.id, [item])
    with store._conv_session("test_encoded_pending_payload") as db:
        encoded = db.scalar(select(SqlConversationItem.data))
        assert "private startup instruction" not in encoded
        assert "chosen-model" not in encoded
    restarted = EncodedStore(db_uri)
    assert restarted.list_pending_inputs(session.id) == [saved]
    assert restarted.transition_pending_input(
        session.id, saved.id, expected="in_progress", status="incomplete"
    )
    [claimed] = restarted.list_pending_inputs(session.id)
    assert claimed.data == saved.data and claimed.status == "incomplete"
    [receipt] = restarted.append(
        session.id,
        [
            NewConversationItem(
                type="message",
                response_id="turn",
                stable_id=saved.id,
                replaces_pending_id=saved.id,
                data=saved.data.model_copy(update={"startup_options": None}),
            )
        ],
    )
    assert restarted.list_pending_inputs(session.id) == []
    assert restarted.list_items(session.id).data == [receipt]


def test_pending_input_pages_continue_in_submission_order(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation()
    saved = store.append(session.id, [_input() for _ in range(5)])
    first, cursor = store.list_pending_inputs_page(session.id, limit=2)
    second, cursor = store.list_pending_inputs_page(session.id, after=cursor, limit=2)
    third, end = store.list_pending_inputs_page(session.id, after=cursor, limit=2)
    assert end is None
    assert [item.id for item in first + second + third] == [item.id for item in saved]
    exact, end = store.list_pending_inputs_page(session.id, limit=5)
    assert len(exact) == 5 and end is None
    assert [item.id for item in store.list_pending_inputs(session.id)] == [
        item.id for item in saved
    ]
