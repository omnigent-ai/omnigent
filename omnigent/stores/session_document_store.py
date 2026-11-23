"""Atomic shared document membership on the session metadata row."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from omnigent.db import compression
from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)
from omnigent.session_documents import (
    SessionDocumentEntry,
    ordered_session_documents,
    validate_google_docs_url,
)

MAX_DOCUMENTS_PER_SESSION = 50
_MAX_JSON_BYTES = 64_000
_MAX_STORED_BYTES = 16_000
_ENTRIES = TypeAdapter(list[SessionDocumentEntry])


class SessionDocumentLimitError(Exception):
    """Another reference would exceed the collection or storage budget."""


class UnreadableSessionDocumentsError(Exception):
    """Unrecognized stored data must never be replaced by a partial collection."""


def _decode(value: str | None) -> list[SessionDocumentEntry]:
    if value is None:
        return []
    try:
        entries = _ENTRIES.validate_json(value)
        ids = [validate_google_docs_url(entry.document_url).document_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate stored document identities")
        return ordered_session_documents(entries)
    except (ValidationError, ValueError) as exc:
        raise UnreadableSessionDocumentsError("Unreadable shared session documents") from exc


def _encode(entries: list[SessionDocumentEntry]) -> str:
    value = _ENTRIES.dump_json(ordered_session_documents(entries)).decode("utf-8")
    encoded = compression.encode(value)
    if (
        len(entries) > MAX_DOCUMENTS_PER_SESSION
        or len(value.encode("utf-8")) > _MAX_JSON_BYTES
        or encoded is None
        or len(encoded) > _MAX_STORED_BYTES
    ):
        raise SessionDocumentLimitError("Session document collection is full")
    return value


class SessionDocumentStore:
    """Shared references scoped by the validated workspace and canonical session ID."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_document_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_document_store", immediate=True
        )

    def _load(
        self, session: Session, session_id: str, *, lock: bool
    ) -> list[SessionDocumentEntry] | None:
        stmt = select(SqlConversationMetadata.session_documents).where(
            SqlConversationMetadata.workspace_id == current_workspace_id(),
            SqlConversationMetadata.id == session_id,
        )
        row = session.execute(stmt.with_for_update() if lock else stmt).one_or_none()
        return None if row is None else _decode(row.session_documents)

    def read(self, session_id: str) -> list[SessionDocumentEntry] | None:
        with self._session("get_session_documents") as session:
            return self._load(session, session_id, lock=False)

    def _update(
        self,
        operation: str,
        session_id: str,
        change: Callable[[list[SessionDocumentEntry]], list[SessionDocumentEntry]],
    ) -> list[SessionDocumentEntry] | None:
        def write(session: Session) -> list[SessionDocumentEntry] | None:
            current = self._load(session, session_id, lock=True)
            if current is None:
                return None
            updated = change(current)
            if updated != current:
                session.execute(
                    update(SqlConversationMetadata)
                    .where(
                        SqlConversationMetadata.workspace_id == current_workspace_id(),
                        SqlConversationMetadata.id == session_id,
                    )
                    .values(session_documents=_encode(updated))
                )
            return ordered_session_documents(updated)

        # SQLite serializes before the read; other engines lock the row and replay
        # serialization failures/deadlocks. Return only after the transaction commits.
        return run_write_transaction(self._session_immediate, operation, write)

    def add(self, session_id: str, url: str) -> list[SessionDocumentEntry] | None:
        reference = validate_google_docs_url(url)
        original_url = url.strip()
        # Validate the original URL's bound even when its canonical form is shorter.
        SessionDocumentEntry(document_url=original_url, added_at=1)

        def add_entry(current: list[SessionDocumentEntry]) -> list[SessionDocumentEntry]:
            if any(
                validate_google_docs_url(entry.document_url).document_id == reference.document_id
                for entry in current
            ):
                return current
            return [
                *current,
                SessionDocumentEntry(document_url=original_url, added_at=now_epoch_us()),
            ]

        return self._update("add_session_document", session_id, add_entry)

    def remove(self, session_id: str, document_id: str) -> list[SessionDocumentEntry] | None:
        return self._update(
            "remove_session_document",
            session_id,
            lambda current: [
                entry
                for entry in current
                if validate_google_docs_url(entry.document_url).document_id != document_id
            ],
        )
