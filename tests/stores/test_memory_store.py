"""Tests for :class:`SqlAlchemyMemoryStore`."""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from omnigent.db.db_models import MEMORY_CONTENT_MAX_STORED_BYTES, SqlMemory
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.memory_store import MemoryStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from tests.stores.memory_store_conformance import (
    ALICE,
    MemoryStoreConformance,
    largest_hard_to_compress_content,
)


@pytest.fixture()
def store(db_uri: str) -> MemoryStore:
    """A store on the per-test database, which the root fixture has migrated."""
    return SqlAlchemyMemoryStore(db_uri)


class TestSqlAlchemyMemoryStore(MemoryStoreConformance):
    """The shared backend contract against SQLAlchemy."""

    def test_lists_in_path_order_across_pages(self, store: MemoryStore) -> None:
        for path in ("/b", "/a/z", "/a", "/c", "/a/b"):
            store.write(ALICE, path, "x")

        first = store.list(ALICE, page_size=3)
        second = store.list(ALICE, page_size=3, page_token=first.next_page_token)

        assert [e.path for e in first.entries] == ["/a", "/a/b", "/a/z"]
        assert [e.path for e in second.entries] == ["/b", "/c"]
        assert second.next_page_token is None

    def test_paths_compare_byte_for_byte(self, store: MemoryStore) -> None:
        store.write(ALICE, "/Projects/api", "upper")
        store.write(ALICE, "/projects/api", "lower")

        assert [e.path for e in store.list(ALICE, path_prefix="/projects").entries] == [
            "/projects/api"
        ]
        loaded = store.get(ALICE, "/Projects/api")
        assert loaded is not None
        assert loaded.content == "upper"

    def test_a_concurrent_first_write_becomes_a_replacement(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another writer inserts the path between this write's read and its insert."""
        first = store.write(ALICE, "/race", "theirs")
        real_get = Session.get
        misses = []

        def stale_get(session: Session, entity, ident, **kwargs):
            # The first read misses the row the other writer committed.
            if entity is SqlMemory and not misses:
                misses.append(ident)
                return None
            return real_get(session, entity, ident, **kwargs)

        monkeypatch.setattr(Session, "get", stale_get)

        written = store.write(ALICE, "/race", "ours")

        assert misses
        assert written.content == "ours"
        assert written.created_at == first.created_at
        loaded = store.get(ALICE, "/race")
        assert loaded is not None
        assert loaded.content == "ours"

    def test_search_is_not_offered(self, store: MemoryStore) -> None:
        store.write(ALICE, "/a", "hello")

        assert store.supports_search is False
        with pytest.raises(OmnigentError) as excinfo:
            store.search(ALICE, "hello")
        assert excinfo.value.code == ErrorCode.NOT_FOUND

    def test_content_is_stored_compressed_within_the_column_bound(
        self, store: MemoryStore, db_uri: str
    ) -> None:
        store.write(ALICE, "/big", largest_hard_to_compress_content())

        with get_or_create_engine(db_uri).connect() as connection:
            stored = connection.execute(
                sa.select(sa.type_coerce(SqlMemory.content, sa.LargeBinary)).where(
                    SqlMemory.path == "/big"
                )
            ).scalar_one()
        assert len(stored) <= MEMORY_CONTENT_MAX_STORED_BYTES

    def test_every_listing_query_reads_one_primary_key_range(
        self, store: MemoryStore, db_uri: str
    ) -> None:
        engine = get_or_create_engine(db_uri)
        if engine.dialect.name != "sqlite":
            pytest.skip("reads SQLite's query plans")
        for path in ("/p", "/p/a", "/p/b", "/p/c", "/q"):
            store.write(ALICE, path, "x")
        statements: list[tuple[str, object]] = []

        def record(_conn, _cursor, statement, parameters, _context, _executemany) -> None:
            if "FROM memories" in statement:
                statements.append((statement, parameters))

        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            first = store.list(ALICE, path_prefix="/p", page_size=2)
            store.list(ALICE, path_prefix="/p", page_size=2, page_token=first.next_page_token)
            store.list(ALICE, page_size=2, page_token="/p/a")
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        with engine.connect() as connection:
            plans = [
                [row[-1] for row in connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}", args)]
                for sql, args in statements
            ]

        # One indexed search per query: no full partition scan and no sort step.
        bounded = re.compile(
            r"SEARCH (TABLE )?memories USING (PRIMARY KEY|INDEX \w+) "
            r"\(workspace_id=\? AND actor_id=\? AND path(=\?|>\?( AND path<\?)?)\)"
        )
        assert len(plans) == 4
        assert all(len(plan) == 1 and bounded.fullmatch(plan[0]) for plan in plans), plans
