"""The shared JSON bound protects every database backend from oversized overrides."""

import json

import pytest

from omnigent.db.db_models import SqlConversation
from omnigent.errors import OmnigentError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize("prefix", ["", "模型", 'a"b\\c'])
def test_encoded_override_limit_preserves_prior_row(
    conversation_store: SqlAlchemyConversationStore, prefix: str
) -> None:
    store = conversation_store
    conv = store.create_conversation(title="original")
    limit = SqlConversation.__table__.c.session_overrides.type.length
    overhead = len(json.dumps({"model_override": prefix}, separators=(",", ":")))
    model = prefix + "a" * (limit - overhead)
    store.update_conversation(conv.id, model_override=model)
    assert store.get_conversation(conv.id).model_override == model
    with pytest.raises(OmnigentError, match="Session overrides exceed"):
        store.update_conversation(conv.id, model_override=model + "a", title="rejected")
    saved = store.get_conversation(conv.id)
    assert saved.model_override == model
    assert saved.title == "original"
