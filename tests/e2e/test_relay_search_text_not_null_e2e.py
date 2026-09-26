"""Store and relay persistence must support items without searchable plaintext.

An opaque-data store subclass returns None from _item_search_text against
the real SQLite schema. Direct append and resource-teardown relay events
must persist without NOT NULL errors; no live model is involved."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.entities.conversation import ResourceEventData
from omnigent.server.routes.sessions import _relay_persist
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_RELAY_LOGGER = "omnigent.server.routes.sessions"


class _SearchTextlessStore(SqlAlchemyConversationStore):
    """Model an opaque-data store by omitting searchable plaintext."""

    def _item_search_text(self, item: NewConversationItem) -> str | None:
        """Return ``None`` -- opaque storage cannot index a plaintext body."""
        return None


def _make_opaque_store(tmp_path: Path) -> _SearchTextlessStore:
    """
    Build a fresh SQLite-backed store that omits ``search_text`` per the seam.

    :param tmp_path: Per-test temp directory for the SQLite file.
    :returns: A store on the mainline schema whose ``_item_search_text``
        returns ``None``.
    """
    uri = f"sqlite:///{tmp_path / 'conversations.db'}"
    get_or_create_engine(uri)
    return _SearchTextlessStore(uri)


@contextmanager
def _capture_relay_errors() -> Iterator[list[logging.LogRecord]]:
    """Capture relay-persistence errors directly, restoring the logger afterward."""
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(_RELAY_LOGGER)
    handler = _Collector(level=logging.ERROR)
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)


def test_append_persists_when_store_omits_search_text(tmp_path: Path) -> None:
    """Persist a user message when the store omits search_text."""
    store = _make_opaque_store(tmp_path)
    conv = store.create_conversation(title="repro search_text NOT NULL")
    item = NewConversationItem(
        type="message",
        response_id="turn_repro",
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "reply with exactly: LOCAL_OK"}],
        ),
    )

    try:
        store.append(conv.id, [item])
    except IntegrityError as exc:
        first_line = str(exc).splitlines()[0]
        assert "conversation_items.search_text" in str(exc)
        pytest.fail(
            "bug is live: append() aborted with a NOT NULL constraint "
            f"failure on conversation_items.search_text -- {first_line}"
        )

    items = store.list_items(conv.id, limit=50).data
    assert [i.type for i in items] == ["message"], (
        "the user message did not persist through a search_text-less store"
    )


def test_relay_persist_survives_search_textless_store(tmp_path: Path) -> None:
    """Persist resource teardown through the relay without swallowing an INSERT error."""
    store = _make_opaque_store(tmp_path)
    conv = store.create_conversation(title="repro search_text NOT NULL")
    item = NewConversationItem(
        type="resource_event",
        response_id=conv.id,
        data=ResourceEventData(
            event_type="session.resource.deleted",
            resource_id="terminal_claude_main",
            resource_type="terminal",
        ),
    )

    with _capture_relay_errors() as records:
        asyncio.run(_relay_persist(store, conv.id, item))

    relay_failures = [r for r in records if "Relay persist failed for session=" in r.getMessage()]
    items = store.list_items(conv.id, limit=50).data

    assert not relay_failures, (
        "bug is live: _relay_persist swallowed a NOT NULL search_text "
        f"IntegrityError -- {relay_failures[0].getMessage() if relay_failures else ''}"
    )
    assert [i.type for i in items] == ["resource_event"], (
        "the resource.deleted event did not persist through a search_text-less store"
    )
