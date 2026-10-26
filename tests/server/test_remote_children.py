"""Execution-bound remote receipts using real transactional conversation stores."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from omnigent.entities import ErrorData, MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.remote_child_protocol import (
    REMOTE_CHILD_STATE,
    SUBAGENT_DELIVERED_ID_LABEL_KEY,
    remote_child_state,
)
from omnigent.server.remote_children import (
    claim_remote_dispatch,
    confirm_remote_stop,
    record_remote_turn,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def remote_child(db_uri: str) -> tuple[SqlAlchemyConversationStore, str]:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        host_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        workspace="/work/repo",
    )
    return store, child.id


def test_remote_receipt_rejects_stale_or_unconfirmed_completion(remote_child, db_uri: str) -> None:
    store, child = remote_child
    claim_remote_dispatch(store, child, "dispatch_a")
    record_remote_turn(store, child, response_id=None, status="completed")
    record_remote_turn(store, child, response_id="turn_a", status="running")
    record_remote_turn(store, child, response_id="other_turn", status="completed")
    assert remote_child_state(store.get_conversation(child).labels)["status"] == "running"
    record_remote_turn(store, child, response_id="turn_a", status="completed", allow_empty=True)
    with pytest.raises(OmnigentError):
        claim_remote_dispatch(store, child, "dispatch_b")
    store.set_labels(child, {SUBAGENT_DELIVERED_ID_LABEL_KEY: "dispatch_a"})
    claim_remote_dispatch(store, child, "dispatch_b")
    record_remote_turn(store, child, response_id="turn_a", status="running")
    record_remote_turn(store, child, response_id="turn_a", status="completed")
    assert remote_child_state(store.get_conversation(child).labels)["status"] == "pending"
    record_remote_turn(store, child, response_id="turn_b", status="running")
    record_remote_turn(store, child, response_id="turn_b", status="failed")
    # A different replica/process reads the same durable result, not a local cache.
    state = remote_child_state(SqlAlchemyConversationStore(db_uri).get_conversation(child).labels)
    assert state["dispatch_id"] == "dispatch_b" and state["status"] == "failed"


def test_remote_dispatch_claim_is_single_writer(remote_child) -> None:
    store, child = remote_child

    def claim(name: str) -> bool:
        try:
            claim_remote_dispatch(store, child, name)
            return True
        except OmnigentError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["dispatch_a", "dispatch_b"]))
    assert sum(outcomes) == 1


def test_remote_dispatch_cannot_claim_a_root(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation()
    with pytest.raises(OmnigentError):
        claim_remote_dispatch(store, root.id, "dispatch_a")


@pytest.mark.parametrize("closed", [False, True])
def test_remote_dispatch_rejects_corrupt_receipt_or_archived_child(remote_child, closed) -> None:
    store, child = remote_child
    if closed:
        store.update_conversation(child, archived=True)
    else:
        store.set_labels(child, {REMOTE_CHILD_STATE: "{broken"})
    with pytest.raises(OmnigentError):
        claim_remote_dispatch(store, child, "dispatch_a")


def test_remote_dispatch_id_is_never_replayed_after_confirmed_stop(remote_child) -> None:
    store, child = remote_child
    dispatch = "a" * 48
    claim_remote_dispatch(store, child, dispatch)
    confirm_remote_stop(store, child)
    assert remote_child_state(store.get_conversation(child).labels)["status"] == "cancelled"
    with pytest.raises(OmnigentError):
        claim_remote_dispatch(store, child, dispatch)
    store.set_labels(child, {SUBAGENT_DELIVERED_ID_LABEL_KEY: dispatch})
    claim_remote_dispatch(store, child, "dispatch_b")


def test_completion_waits_for_matching_durable_transcript(remote_child) -> None:
    store, child = remote_child
    claim_remote_dispatch(store, child, "dispatch_a")
    record_remote_turn(store, child, response_id="turn_a", status="running")
    store.append(
        child,
        [
            NewConversationItem(
                type="message",
                response_id="old_turn",
                data=MessageData(role="assistant", agent="worker", content=[]),
            )
        ],
    )
    assert record_remote_turn(store, child, response_id="turn_a", status="completed") is False
    item = store.append(
        child,
        [
            NewConversationItem(
                type="message",
                response_id="turn_a",
                data=MessageData(role="assistant", agent="worker", content=[]),
            )
        ],
    )[0]
    assert record_remote_turn(store, child, response_id="turn_a", status="completed") is True
    state = remote_child_state(store.get_conversation(child).labels)
    assert state["result_item_id"] == item.id


@pytest.mark.asyncio
@pytest.mark.parametrize("already_claimed", [False, True])
async def test_remote_native_boot_failure_commits_failed_receipt(
    remote_child, already_claimed
) -> None:
    from omnigent.server.routes._sessions.orchestration import _persist_native_terminal_failure
    from omnigent.server.schemas import SessionEventInput

    store, child = remote_child
    if already_claimed:
        claim_remote_dispatch(store, child, "dispatch_a")
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "build"}],
            "remote_dispatch_id": "dispatch_a",
        },
    )
    await _persist_native_terminal_failure(
        child,
        store.get_conversation(child),
        body,
        store,
        ErrorData(
            source="execution",
            code="native_terminal_start_failed",
            message="Native CLI unavailable",
        ),
        None,
        created_by=None,
    )
    state = remote_child_state(store.get_conversation(child).labels)
    assert state["dispatch_id"] == "dispatch_a" and state["status"] == "failed"
    result = next(
        item for item in store.list_items(child).data if item.id == state["result_item_id"]
    )
    assert isinstance(result.data, ErrorData) and result.data.message == "Native CLI unavailable"
