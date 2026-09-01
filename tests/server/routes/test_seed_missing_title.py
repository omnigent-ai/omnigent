"""The deterministic title seeder never renames a session a human named."""

from __future__ import annotations

import pytest

from omnigent.server.routes._sessions.helpers import _seed_missing_title
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


async def test_seed_names_an_untitled_session(db_uri: str) -> None:
    """An untitled session takes its first prompt as its name."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")

    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": "refactor the auth module"}],
        store,
    )

    assert conv.title == "refactor the auth module"
    stored = store.get_conversation(conv.id)
    assert stored is not None
    assert stored.title == "refactor the auth module"


async def test_seed_loses_to_a_rename_that_landed_after_the_row_was_read(db_uri: str) -> None:
    """A rename committed after the route read ``conv`` survives the seed.

    ``conv`` is the row as of the route boundary, so its in-memory ``title``
    still reads untitled. The write is a compare-and-swap against the untitled
    sentinel for exactly this reason.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    store.update_conversation(conv.id, title="Auth work")
    assert conv.title is None

    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": "refactor the auth module"}],
        store,
    )

    stored = store.get_conversation(conv.id)
    assert stored is not None
    assert stored.title == "Auth work"
