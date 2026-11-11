"""Archived-state reads stay bounded by their ids and never load labels."""

from uuid import uuid4

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.conversation_store import sqlalchemy_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_reads_only_the_archived_flag_of_the_given_sessions(db_uri, monkeypatch):
    store = SqlAlchemyConversationStore(db_uri)
    kept = store.create_conversation(labels={"team": "a"})
    archived = store.create_conversation(labels={"team": "b"})
    store.update_conversation(archived.id, archived=True)

    def no_labels(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("archived-state reads must not load labels")

    monkeypatch.setattr(sqlalchemy_store, "_fetch_labels_bulk", no_labels)
    assert store.get_archived_states([kept.id, archived.id, uuid4().hex]) == {
        kept.id: False,
        archived.id: True,
    }
    assert store.get_archived_states([]) == {}
    with workspace_scope(17):
        assert store.get_archived_states([kept.id]) == {}
    with pytest.raises(ValueError, match="at most 1000"):
        store.get_archived_states([kept.id] * 1001)
