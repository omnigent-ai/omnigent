"""Store contract for the Design page deck index (``design_artifacts``)."""

from __future__ import annotations

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.entities.design_artifact import design_artifact_kind
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("decks/q3.slides.html", "deck"),
        ("ux/flow.wireframe.html", "wireframe"),
        ("notes.html", None),
        (".slides.html", None),
        ("a/.wireframe.html", None),
    ],
)
def test_design_artifact_kind(path: str, kind: str | None) -> None:
    assert design_artifact_kind(path) == kind


def _live(store: SqlAlchemyConversationStore, kind: str | None = None) -> list[tuple[str, str]]:
    return [(a.session_id, a.path) for a in store.list_design_artifacts(kind=kind)]


def test_record_upserts_and_lists_newest_first(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    a = conversation_store.create_conversation()
    b = conversation_store.create_conversation()
    conversation_store.record_design_artifact(a.id, "q3.slides.html", "deck", now=100)
    conversation_store.record_design_artifact(b.id, "flow.wireframe.html", "wireframe", now=200)
    conversation_store.record_design_artifact(a.id, "q3.slides.html", "deck", now=300)

    rows = conversation_store.list_design_artifacts()
    assert [(r.session_id, r.path, r.kind, r.updated_at) for r in rows] == [
        (a.id, "q3.slides.html", "deck", 300),
        (b.id, "flow.wireframe.html", "wireframe", 200),
    ]
    assert _live(conversation_store, kind="wireframe") == [(b.id, "flow.wireframe.html")]


def test_deleted_marks_existing_row_and_never_inserts(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.record_design_artifact(conv.id, "a.slides.html", "deck", now=1)
    conversation_store.record_design_artifact(conv.id, "a.slides.html", "deck", deleted=True)
    conversation_store.record_design_artifact(conv.id, "never.slides.html", "deck", deleted=True)
    assert _live(conversation_store) == []

    # A later write brings the deck back.
    conversation_store.record_design_artifact(conv.id, "a.slides.html", "deck", now=5)
    assert _live(conversation_store) == [(conv.id, "a.slides.html")]


def test_replace_sets_exactly_the_given_paths(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    other = conversation_store.create_conversation()
    conversation_store.record_design_artifact(conv.id, "keep.slides.html", "deck", now=10)
    conversation_store.record_design_artifact(conv.id, "gone.slides.html", "deck", now=10)
    conversation_store.record_design_artifact(conv.id, "back.slides.html", "deck", deleted=True)
    conversation_store.record_design_artifact(other.id, "other.slides.html", "deck", now=10)

    conversation_store.replace_design_artifacts(
        conv.id, ["keep.slides.html", "new.wireframe.html"], now=50
    )

    rows = {(r.session_id, r.path): r for r in conversation_store.list_design_artifacts()}
    assert set(rows) == {
        (conv.id, "keep.slides.html"),
        (conv.id, "new.wireframe.html"),
        (other.id, "other.slides.html"),
    }
    # An unchanged deck keeps its time; a newly found one gets the reconcile time.
    assert rows[(conv.id, "keep.slides.html")].updated_at == 10
    assert rows[(conv.id, "new.wireframe.html")].updated_at == 50
    assert rows[(conv.id, "new.wireframe.html")].kind == "wireframe"


def test_replace_of_one_kind_leaves_the_other(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.record_design_artifact(conv.id, "a.slides.html", "deck", now=1)
    conversation_store.record_design_artifact(conv.id, "w.wireframe.html", "wireframe", now=1)

    conversation_store.replace_design_artifacts(
        conv.id, ["b.slides.html", "x.wireframe.html"], kind="deck", now=2
    )

    assert sorted(_live(conversation_store)) == [
        (conv.id, "b.slides.html"),
        (conv.id, "w.wireframe.html"),
    ]


def test_rows_are_workspace_scoped(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.record_design_artifact(conv.id, "a.slides.html", "deck", now=1)
    with workspace_scope(7):
        assert conversation_store.list_design_artifacts() == []


def test_split_db_store_lists_artifacts(
    split_db_conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = split_db_conversation_store.create_conversation()
    split_db_conversation_store.record_design_artifact(conv.id, "a.slides.html", "deck", now=1)
    assert _live(split_db_conversation_store) == [(conv.id, "a.slides.html")]


def test_list_conversations_filters_to_given_ids(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    a = conversation_store.create_conversation()
    conversation_store.create_conversation()
    page = conversation_store.list_conversations(conversation_ids=[a.id])
    assert [c.id for c in page.data] == [a.id]
    assert conversation_store.list_conversations(conversation_ids=[]).data == []


async def test_delete_conversation_removes_the_subtree_rows(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(parent_conversation_id=parent.id)
    other = conversation_store.create_conversation()
    for conv in (parent, child, other):
        conversation_store.record_design_artifact(conv.id, "a.slides.html", "deck", now=1)

    assert await conversation_store.delete_conversation(parent.id)

    assert _live(conversation_store) == [(other.id, "a.slides.html")]


def test_conversation_ids_only_narrow_accessible_by(db_uri: str) -> None:
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("alice")
    mine = store.create_conversation()
    also_mine = store.create_conversation()
    not_mine = store.create_conversation()
    permissions.grant("alice", mine.id, LEVEL_OWNER)
    permissions.grant("alice", also_mine.id, LEVEL_OWNER)

    page = store.list_conversations(
        accessible_by="alice", conversation_ids=[mine.id, not_mine.id], limit=10
    )
    assert [c.id for c in page.data] == [mine.id]
