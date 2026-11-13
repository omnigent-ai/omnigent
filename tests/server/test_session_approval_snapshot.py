"""Pending approval snapshots visit only branches with indexed prompts."""

from __future__ import annotations

from itertools import count
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, update

from omnigent.db.db_models import SqlConversation, SqlConversationMetadata, current_workspace_id
from omnigent.db.utils import get_or_create_engine
from omnigent.entities import Conversation
from omnigent.runtime import pending_elicitations, session_stream
from omnigent.server.routes._sessions import helpers
from omnigent.stores.conversation_store import sqlalchemy_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


def _record_pending(
    session_id: str, elicitation_id: str, *, target_session_id: str | None = None
) -> dict[str, Any]:
    params = {"mode": "form", "message": elicitation_id}
    if target_session_id is not None:
        params["target_session_id"] = target_session_id
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": params,
    }
    pending_elicitations.record_publish(session_id, event)
    return event


def _spy_reads(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[Mock, Mock, Mock]:
    listing = Mock(wraps=store.list_conversations)
    batch = Mock(wraps=store.get_conversations)
    single = Mock(wraps=store.get_conversation)
    monkeypatch.setattr(store, "list_conversations", listing)
    monkeypatch.setattr(store, "get_conversations", batch)
    monkeypatch.setattr(store, "get_conversation", single)
    return listing, batch, single


@pytest.mark.parametrize("pending_scope", ["none", "own", "unrelated"])
def test_snapshot_skips_tree_without_pending_descendants(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    pending_scope: str,
) -> None:
    parent = store.create_conversation()
    idle = store.create_conversation(parent_conversation_id=parent.id)
    store.create_conversation(parent_conversation_id=idle.id)
    other = store.create_conversation()
    unrelated = store.create_conversation(parent_conversation_id=other.id)
    expected = []
    if pending_scope == "own":
        expected = [_record_pending(parent.id, "own")]
    elif pending_scope == "unrelated":
        _record_pending(unrelated.id, "unrelated")
    listing, batch, single = _spy_reads(store, monkeypatch)

    assert helpers._pending_elicitation_snapshot_for_session(store, parent) == expected

    listing.assert_not_called()
    single.assert_not_called()
    if pending_scope == "unrelated":
        requested = [session_id for call in batch.call_args_list for session_id in call.args[0]]
        assert requested == [unrelated.id]
        assert batch.call_count == 1
    else:
        batch.assert_not_called()


def test_snapshot_bounds_pending_ancestry_reads(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = store.create_conversation()
    unrelated = store.create_conversation()
    own = _record_pending(parent.id, "own")
    pending_ids = {f"{index:032x}" for index in range(5000)}
    pending_ids.add(unrelated.id)
    for session_id in pending_ids:
        _record_pending(session_id, session_id)
    read_batch = store.get_conversations
    listing, batch, single = _spy_reads(store, monkeypatch)

    def bounded_read(ids: list[str]) -> dict[str, Conversation]:
        if len(ids) > 5000:
            raise RuntimeError("Bulk Fetch request limit exceeded")
        return read_batch(ids)

    batch.side_effect = bounded_read

    assert helpers._pending_elicitation_snapshot_for_session(store, parent) == [own]

    requested = [session_id for call in batch.call_args_list for session_id in call.args[0]]
    assert set(requested) == pending_ids
    assert len(requested) == len(pending_ids)
    assert all(len(call.args[0]) <= 5000 for call in batch.call_args_list)
    listing.assert_not_called()
    single.assert_not_called()


def test_wide_snapshot_prunes_idle_leaves_and_preserves_paginated_bfs(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamps = count(1_700_000_000)
    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: next(timestamps))
    parent = store.create_conversation()
    older_branch = store.create_conversation(parent_conversation_id=parent.id)
    middle = store.create_conversation(parent_conversation_id=older_branch.id)
    deep = store.create_conversation(parent_conversation_id=middle.id)
    direct = store.create_conversation(parent_conversation_id=parent.id)
    idle_ids = {store.create_conversation(parent_conversation_id=parent.id).id for _ in range(105)}
    newer_branch = store.create_conversation(parent_conversation_id=parent.id)
    shallow = store.create_conversation(parent_conversation_id=newer_branch.id)
    store.set_session_live_status(deep.id, "idle")
    for session in (deep, shallow, direct, newer_branch):
        _record_pending(session.id, session.id)
    first_page = store.list_conversations(
        kind="sub_agent", parent_conversation_id=parent.id, limit=100
    )
    assert first_page.has_more
    assert direct.id not in {row.id for row in first_page.data}
    listing, batch, single = _spy_reads(store, monkeypatch)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    assert [event["elicitation_id"] for event in events] == [
        newer_branch.id,
        direct.id,
        shallow.id,
        deep.id,
    ]
    assert [event["params"]["target_session_id"] for event in events] == [
        newer_branch.id,
        direct.id,
        shallow.id,
        deep.id,
    ]
    assert [call.kwargs["parent_conversation_id"] for call in listing.call_args_list] == [
        parent.id,
        parent.id,
        newer_branch.id,
        older_branch.id,
        middle.id,
    ]
    assert [call.kwargs["after"] for call in listing.call_args_list] == [
        None,
        first_page.last_id,
        None,
        None,
        None,
    ]
    assert all(call.kwargs["kind"] == "sub_agent" for call in listing.call_args_list)
    assert all(call.kwargs["limit"] == 100 for call in listing.call_args_list)
    requested = [session_id for call in batch.call_args_list for session_id in call.args[0]]
    assert set(requested) == {
        direct.id,
        shallow.id,
        deep.id,
        newer_branch.id,
        older_branch.id,
        middle.id,
    }
    assert len(requested) == 6
    assert not idle_ids.intersection(requested)
    single.assert_not_called()


def test_snapshot_keeps_own_mirror_and_deduplicates_nested_approvals(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = store.create_conversation()
    branch = store.create_conversation(parent_conversation_id=parent.id)
    leaf = store.create_conversation(parent_conversation_id=branch.id)
    own = _record_pending(parent.id, "own")
    mirrored = _record_pending(parent.id, "shared", target_session_id=leaf.id)
    _record_pending(branch.id, "shared", target_session_id=leaf.id)
    child = _record_pending(branch.id, "child")
    _record_pending(leaf.id, "shared")
    nested = _record_pending(leaf.id, "nested")
    listing, batch, single = _spy_reads(store, monkeypatch)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    assert events == [
        own,
        mirrored,
        {**child, "params": {**child["params"], "target_session_id": branch.id}},
        {**nested, "params": {**nested["params"], "target_session_id": leaf.id}},
    ]
    assert pending_elicitations.snapshot_for(parent.id) == [own, mirrored]
    assert pending_elicitations.snapshot_for(branch.id)[-1] == child
    assert pending_elicitations.snapshot_for(leaf.id)[-1] == nested
    assert "target_session_id" not in child["params"]
    assert "target_session_id" not in nested["params"]
    assert [call.kwargs["parent_conversation_id"] for call in listing.call_args_list] == [
        parent.id,
        branch.id,
    ]
    assert batch.call_count == 1
    single.assert_not_called()


@pytest.mark.parametrize(
    "excluded",
    ["archived-ancestor", "archived-pending", "missing-ancestor", "missing-pending", "unreadable"],
)
def test_snapshot_excludes_broken_paths_and_unrelated_siblings(
    store: SqlAlchemyConversationStore,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    excluded: str,
) -> None:
    root = store.create_conversation()
    parent = store.create_conversation(parent_conversation_id=root.id)
    sibling = store.create_conversation(parent_conversation_id=root.id)
    blocked = store.create_conversation(parent_conversation_id=parent.id)
    hidden = store.create_conversation(parent_conversation_id=blocked.id)
    visible = store.create_conversation(parent_conversation_id=parent.id)
    for session in (hidden, sibling, visible):
        _record_pending(session.id, session.id)
    affected = hidden if excluded.endswith("pending") else blocked
    if excluded.startswith("archived"):
        store.update_conversation(affected.id, archived=True)
    elif excluded.startswith("missing"):
        with get_or_create_engine(db_uri).begin() as connection:
            connection.execute(
                delete(SqlConversation).where(
                    SqlConversation.workspace_id == current_workspace_id(),
                    SqlConversation.id == affected.id,
                )
            )
    read_batch = store.get_conversations
    listing, batch, single = _spy_reads(store, monkeypatch)
    if excluded == "unreadable":

        def omit_unreadable(ids: list[str]) -> dict[str, Conversation]:
            rows = read_batch(ids)
            rows.pop(blocked.id, None)
            return rows

        batch.side_effect = omit_unreadable

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    assert [event["elicitation_id"] for event in events] == [visible.id]
    assert events[0]["params"]["target_session_id"] == visible.id
    assert [call.kwargs["parent_conversation_id"] for call in listing.call_args_list] == [
        parent.id
    ]
    single.assert_not_called()


@pytest.mark.parametrize("reachable", [False, True], ids=["unrelated-cycle", "ancestor-cycle"])
def test_snapshot_terminates_cycles_without_listing_pending_leaves(
    store: SqlAlchemyConversationStore,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    reachable: bool,
) -> None:
    parent = store.create_conversation()
    cycle_root = parent if reachable else store.create_conversation()
    branch = store.create_conversation(parent_conversation_id=cycle_root.id)
    leaf = store.create_conversation(parent_conversation_id=branch.id)
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            update(SqlConversation)
            .where(
                SqlConversation.workspace_id == current_workspace_id(),
                SqlConversation.id == cycle_root.id,
            )
            .values(parent_conversation_id=leaf.id)
        )
    _record_pending(leaf.id, "cycle")
    listing, batch, single = _spy_reads(store, monkeypatch)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    assert [event["elicitation_id"] for event in events] == (["cycle"] if reachable else [])
    assert [call.kwargs["parent_conversation_id"] for call in listing.call_args_list] == (
        [parent.id, branch.id] if reachable else []
    )
    requested = [session_id for call in batch.call_args_list for session_id in call.args[0]]
    assert len(requested) == len(set(requested)) == (2 if reachable else 1)
    single.assert_not_called()


def test_snapshot_includes_sibling_approval_published_during_listing(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = store.create_conversation()
    first = store.create_conversation(parent_conversation_id=parent.id)
    late = store.create_conversation(parent_conversation_id=parent.id)
    _record_pending(first.id, "first")
    list_conversations = store.list_conversations

    def publish_during_listing(*args: Any, **kwargs: Any) -> Any:
        page = list_conversations(*args, **kwargs)
        _record_pending(late.id, "late")
        return page

    monkeypatch.setattr(store, "list_conversations", publish_during_listing)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    assert {event["elicitation_id"]: event["params"]["target_session_id"] for event in events} == {
        "first": first.id,
        "late": late.id,
    }


@pytest.mark.parametrize("resolve_first", [False, True], ids=["first-pending", "first-resolved"])
def test_snapshot_includes_nested_approval_published_during_root_listing(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    resolve_first: bool,
) -> None:
    parent = store.create_conversation()
    first = store.create_conversation(parent_conversation_id=parent.id)
    idle_branch = store.create_conversation(parent_conversation_id=parent.id)
    late = store.create_conversation(parent_conversation_id=idle_branch.id)
    _record_pending(first.id, "first")
    list_conversations = store.list_conversations

    def publish_during_root_listing(*args: Any, **kwargs: Any) -> Any:
        page = list_conversations(*args, **kwargs)
        if kwargs.get("parent_conversation_id") == parent.id:
            monkeypatch.setattr(store, "list_conversations", list_conversations)
            session_stream.publish(
                late.id,
                {
                    "type": "response.elicitation_request",
                    "elicitation_id": "late",
                    "params": {"mode": "form", "message": "late"},
                },
            )
            if resolve_first:
                session_stream.publish(
                    first.id,
                    {"type": "response.elicitation_resolved", "elicitation_id": "first"},
                )
        return page

    monkeypatch.setattr(store, "list_conversations", publish_during_root_listing)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    expected = {"late": late.id}
    if not resolve_first:
        expected["first"] = first.id
    targets = {event["elicitation_id"]: event["params"]["target_session_id"] for event in events}
    assert targets == expected
    assert len(events) == len(expected)


def test_snapshot_includes_completed_mirror_after_pending_ids_capture(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id)
    unrelated = store.create_conversation()
    _record_pending(unrelated.id, "unrelated")
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "late-direct",
        "params": {"mode": "form", "message": "late-direct"},
    }
    pending_session_ids = pending_elicitations.pending_session_ids

    def capture_then_publish() -> list[str]:
        captured = pending_session_ids()
        monkeypatch.setattr(pending_elicitations, "pending_session_ids", pending_session_ids)
        session_stream.publish(child.id, event)
        helpers._publish_elicitation_request_to_ancestors(store, child.id, event)
        return captured

    monkeypatch.setattr(pending_elicitations, "pending_session_ids", capture_then_publish)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    mirrored = {**event, "params": {**event["params"], "target_session_id": child.id}}
    assert pending_elicitations.snapshot_for(parent.id) == [mirrored]
    assert events == [mirrored]


def test_snapshot_stops_hydration_at_unrelated_pending_chain_leaf(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = store.create_conversation()
    unrelated_root = store.create_conversation()
    leaf = unrelated_root
    for _ in range(12):
        leaf = store.create_conversation(parent_conversation_id=leaf.id)
    assert leaf.root_conversation_id == unrelated_root.id
    assert parent.root_conversation_id == parent.id
    own = _record_pending(parent.id, "own")
    _record_pending(leaf.id, "unrelated")
    listing, batch, single = _spy_reads(store, monkeypatch)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    assert events == [own]
    requested = [session_id for call in batch.call_args_list for session_id in call.args[0]]
    assert requested == [leaf.id]
    assert batch.call_count == 1
    listing.assert_not_called()
    single.assert_not_called()


@pytest.mark.parametrize(
    ("invalid_metadata", "error_type"),
    [({"live_status": 999}, ValueError), ({"session_state": "[]"}, TypeError)],
)
def test_snapshot_preserves_own_prompt_when_unrelated_metadata_is_invalid(
    store: SqlAlchemyConversationStore,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    invalid_metadata: dict[str, Any],
    error_type: type[Exception],
) -> None:
    parent = store.create_conversation()
    unrelated = store.create_conversation()
    own = _record_pending(parent.id, "own")
    _record_pending(unrelated.id, "unrelated")
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            update(SqlConversationMetadata)
            .where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id == unrelated.id,
            )
            .values(**invalid_metadata)
        )
    with pytest.raises(error_type, match=r"live_status|session_state"):
        store.get_conversations([unrelated.id])
    listing, batch, single = _spy_reads(store, monkeypatch)

    events = helpers._pending_elicitation_snapshot_for_session(store, parent)

    assert events == [own]
    assert [call.kwargs["parent_conversation_id"] for call in listing.call_args_list] == [
        parent.id
    ]
    assert batch.call_count == 1
    single.assert_not_called()
    assert any(
        record.exc_info and isinstance(record.exc_info[1], error_type) for record in caplog.records
    )
