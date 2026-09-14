"""E2E: relay/store persistence must survive a search_text-less store.

Reported journey: a session runs turns on a deployed
build whose conversation store stores item ``data`` opaquely and therefore
cannot index a plaintext ``search_text``. As the relay persists conversation
items (a user message, a ``session.resource.deleted`` terminal teardown
event), the INSERT aborts with::

    (sqlite3.IntegrityError) NOT NULL constraint failed: conversation_items.search_text
    [SQL: INSERT INTO conversation_items (workspace_id, conversation_id, id,
     response_id, created_at, status, position, type, data) VALUES (...)]

The user never sees this -- the item silently fails to persist and the failure
is logged server-side (``Relay persist failed for session=...`` /
``_handle_statement_error`` ``Database error: ...``).

Root cause exercised here: ``SqlAlchemyConversationStore`` documents an
overridable ``_item_search_text`` seam -- "A subclass whose schema omits
``search_text`` (e.g. because ``data`` is stored opaquely and cannot be
searched in SQL) returns ``None`` to skip persisting the column and its FTS
row entirely." ``append()`` honors that by dropping the ``search_text`` key
from the batch INSERT. But the mainline schema declares
``conversation_items.search_text`` as ``NOT NULL`` (``db_models.py``:
``search_text: Mapped[str] = mapped_column(Text)``), so a store that uses the
documented seam has the whole INSERT aborted by the constraint -- exactly the
error observed on the deployed opaque-store build.

The default store never returns ``None`` from ``_item_search_text`` (it always
extracts a -- possibly empty -- string), so this reproduction stands the
deployed opaque/encrypted store in with a minimal subclass that returns
``None`` from that documented seam, run against the mainline schema.

The test asserts the *desired* behavior (the items persist), so it FAILS while
the NOT NULL bug is live and passes once the seam and schema are reconciled
(e.g. the column is made nullable, or ``append`` writes ``""`` when the seam
returns ``None``).

Usage::

    pytest tests/e2e/test_relay_search_text_not_null_e2e.py -v
"""

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
    """Stand-in for a deployed opaque/encrypted conversation store.

    Exercises the documented ``_item_search_text`` seam: "A subclass whose
    schema omits ``search_text`` (e.g. because ``data`` is stored opaquely and
    cannot be searched in SQL) returns ``None`` to skip persisting the column
    and its FTS row entirely." It runs against the mainline schema, where
    ``conversation_items.search_text`` is ``NOT NULL``.
    """

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
    """
    Capture ERROR records emitted by the relay-persist logger.

    Attaches a handler directly to ``omnigent.server.routes.sessions`` (the
    logger that emits the relay-persist failure) so the capture is independent
    of pytest's caplog propagation, then restores the logger on exit.

    :returns: A live list of the captured :class:`logging.LogRecord`.
    """
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
    """
    First reported symptom: a direct ``append`` of a user message.

    While the bug is live, the batch INSERT drops the ``search_text`` column
    (the store's ``_item_search_text`` returns ``None``) and the mainline
    schema declares it ``NOT NULL``, so the whole INSERT aborts with the exact
    ``NOT NULL constraint failed: conversation_items.search_text`` the ticket
    quotes. Post-fix, the item persists.
    """
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
    """
    Second reported symptom: the relay persists a resource-teardown event.

    Drives the real ``_relay_persist`` (the function named in the ticket) with
    a ``session.resource.deleted`` terminal event. While the bug is live,
    ``append`` raises the NOT NULL ``search_text`` ``IntegrityError``,
    ``_relay_persist`` swallows it and logs ``Relay persist failed for
    session=...``, and nothing persists. Post-fix, the event is durable and no
    failure is logged.
    """
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
