"""Candidate lookup in the SQLite trigram index of conversation item text.

The index and the triggers that maintain it are created by migration
``ce23c8d4c682``. Search treats it as a candidate filter only: every candidate
is rechecked against the original ``LIKE`` predicate on ``conversation_items``.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import BigInteger, TableClause, bindparam, column, table, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from omnigent.db.db_models import Uuid16

_FTS_TABLE = "conversation_items_trigram_fts"
_ROWS_TABLE = "conversation_items_trigram_rows"
_INDEX_OBJECTS = frozenset(
    {
        _FTS_TABLE,
        _ROWS_TABLE,
        "conversation_items_trigram_insert",
        "conversation_items_trigram_update",
        "conversation_items_trigram_delete",
    }
)
_CANDIDATES_TABLE = "conversation_items_trigram_candidates"
# A search loads at most this many candidate items. Queries matching more fall
# back to the LIKE scan, which finds such common text near the start of the page.
MAX_TRIGRAM_CANDIDATES = 5000
# The trigram tokenizer cannot match fewer than three characters.
_MIN_QUERY_CHARACTERS = 3

trigram_candidates: TableClause = table(
    _CANDIDATES_TABLE,
    column("workspace_id", BigInteger()),
    column("conversation_id", Uuid16()),
    column("item_id", Uuid16()),
    schema="temp",
)


def _trigram_index_ready(session: Session) -> bool:
    """Whether the index exists and every trigger that keeps it current is in place."""
    names = session.execute(
        text("SELECT name FROM sqlite_master WHERE name IN :names").bindparams(
            bindparam("names", sorted(_INDEX_OBJECTS), expanding=True)
        )
    ).scalars()
    return set(names) == _INDEX_OBJECTS


def load_trigram_candidates(session: Session, query: str) -> bool:
    """
    Load the items whose text may contain *query* into ``temp`` candidates.

    :param session: Session on the conversation database. Its connection keeps
        the temporary table, which this call clears first.
    :param query: The search text, matched as a literal substring.
    :returns: ``False`` when the caller must scan instead: the database is not
        SQLite, the index is missing, the query is shorter than a trigram, or it
        matches more than :data:`MAX_TRIGRAM_CANDIDATES` items.
    """
    bind = session.get_bind()
    if bind.dialect.name != "sqlite" or len(query) < _MIN_QUERY_CHARACTERS:
        return False
    if not _trigram_index_ready(session):
        return False
    session.execute(
        text(
            f"CREATE TEMP TABLE IF NOT EXISTS {_CANDIDATES_TABLE} ("
            "workspace_id INTEGER NOT NULL, conversation_id BLOB NOT NULL, "
            "item_id BLOB NOT NULL, PRIMARY KEY (workspace_id, conversation_id, item_id)"
            ") WITHOUT ROWID"
        )
    )
    session.execute(text(f"DELETE FROM temp.{_CANDIDATES_TABLE}"))
    loaded = cast(
        CursorResult[Any],
        session.execute(
            text(
                f"INSERT INTO temp.{_CANDIDATES_TABLE} (workspace_id, conversation_id, item_id) "
                "SELECT indexed.workspace_id, indexed.conversation_id, indexed.item_id "
                f"FROM (SELECT rowid FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH :phrase "
                "LIMIT :limit) AS matched "
                f"JOIN {_ROWS_TABLE} AS indexed ON indexed.fts_rowid = matched.rowid"
            ),
            {
                "phrase": '"' + query.replace('"', '""') + '"',
                "limit": MAX_TRIGRAM_CANDIDATES + 1,
            },
        ),
    )
    return loaded.rowcount <= MAX_TRIGRAM_CANDIDATES
