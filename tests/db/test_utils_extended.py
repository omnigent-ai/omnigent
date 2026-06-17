"""Extended tests for database utilities (omnigent/db/utils.py).

Covers public utilities NOT already tested in test_utils.py:
- normalize_database_url
- engine caching (get_or_create_engine returns same engine for same URI)
- make_managed_session_maker (commit/rollback semantics)
- ID generators (generate_file_id, generate_conversation_id, generate_task_id,
  generate_item_id for all types)
- FTS helpers (ensure_fts_table, insert_fts, delete_fts_by_conversation)
- Timestamp helpers (now_epoch, now_epoch_us, utc_day)
- clear_engine_cache
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from omnigent.db.db_models import SqlUser
from omnigent.db.utils import (
    _ITEM_TYPE_PREFIX,
    clear_engine_cache,
    delete_fts_by_conversation,
    ensure_fts_table,
    generate_conversation_id,
    generate_file_id,
    generate_item_id,
    generate_task_id,
    get_or_create_engine,
    insert_fts,
    make_managed_session_maker,
    normalize_database_url,
    now_epoch,
    now_epoch_us,
    utc_day,
)

# ── normalize_database_url ────────────────────────────


class TestNormalizeDatabaseUrl:
    def test_postgres_prefix(self) -> None:
        url = "postgres://user:pass@host/db"
        assert normalize_database_url(url) == "postgresql+psycopg://user:pass@host/db"

    def test_postgresql_prefix(self) -> None:
        url = "postgresql://user:pass@host/db"
        assert normalize_database_url(url) == "postgresql+psycopg://user:pass@host/db"

    def test_sqlite_passthrough(self) -> None:
        url = "sqlite:///path/to/db.sqlite"
        assert normalize_database_url(url) == url

    def test_already_psycopg_passthrough(self) -> None:
        url = "postgresql+psycopg://user:pass@host/db"
        assert normalize_database_url(url) == url

    def test_empty_string(self) -> None:
        assert normalize_database_url("") == ""

    def test_mysql_passthrough(self) -> None:
        url = "mysql://user:pass@host/db"
        assert normalize_database_url(url) == url


# ── Engine caching ────────────────────────────────────


class TestEngineCaching:
    @pytest.fixture(autouse=True)
    def _clean_cache(self) -> Iterator[None]:
        clear_engine_cache()
        yield
        clear_engine_cache()

    def test_same_uri_returns_same_engine(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cache_test.db"
        uri = f"sqlite:///{db_path}"
        e1 = get_or_create_engine(uri)
        e2 = get_or_create_engine(uri)
        assert e1 is e2

    def test_different_uri_returns_different_engine(self, tmp_path: Path) -> None:
        uri1 = f"sqlite:///{tmp_path / 'a.db'}"
        uri2 = f"sqlite:///{tmp_path / 'b.db'}"
        e1 = get_or_create_engine(uri1)
        e2 = get_or_create_engine(uri2)
        assert e1 is not e2

    def test_clear_engine_cache_removes_engines(self, tmp_path: Path) -> None:
        uri = f"sqlite:///{tmp_path / 'clear.db'}"
        e1 = get_or_create_engine(uri)
        clear_engine_cache()
        e2 = get_or_create_engine(uri)
        assert e1 is not e2


# ── make_managed_session_maker ────────────────────────


class TestManagedSessionMaker:
    def test_auto_commit_on_success(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            session.add(SqlUser(id="commit_test", is_admin=False))

        # Data should be visible in a new session
        with managed() as session:
            loaded = session.get(SqlUser, "commit_test")
            assert loaded is not None

    def test_auto_rollback_on_exception(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with pytest.raises(ValueError):
            with managed() as session:
                session.add(SqlUser(id="rollback_test", is_admin=False))
                raise ValueError("simulated error")

        # Data should NOT be visible
        with managed() as session:
            loaded = session.get(SqlUser, "rollback_test")
            assert loaded is None

    def test_sqlite_foreign_keys_enabled(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            result = session.execute(text("PRAGMA foreign_keys")).scalar()
            assert result == 1

    def test_immediate_mode(self, db_uri: str) -> None:
        """immediate=True should not raise and still commit."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine, immediate=True)

        with managed() as session:
            session.add(SqlUser(id="immediate_test", is_admin=False))

        with managed() as session:
            loaded = session.get(SqlUser, "immediate_test")
            assert loaded is not None


# ── ID generators ─────────────────────────────────────


class TestIdGenerators:
    def test_generate_file_id_format(self) -> None:
        fid = generate_file_id()
        assert re.fullmatch(r"file_[0-9a-f]{32}", fid)

    def test_generate_conversation_id_format(self) -> None:
        cid = generate_conversation_id()
        assert re.fullmatch(r"conv_[0-9a-f]{32}", cid)

    def test_generate_task_id_format(self) -> None:
        tid = generate_task_id()
        assert re.fullmatch(r"resp_[0-9a-f]{32}", tid)

    def test_generate_item_id_all_types(self) -> None:
        for item_type, prefix in _ITEM_TYPE_PREFIX.items():
            item_id = generate_item_id(item_type)
            assert item_id.startswith(prefix), f"{item_type} should start with {prefix}"
            assert len(item_id) == len(prefix) + 32

    def test_generate_item_id_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown item type"):
            generate_item_id("nonexistent_type")

    def test_ids_are_unique(self) -> None:
        ids = {generate_file_id() for _ in range(100)}
        assert len(ids) == 100


# ── FTS helpers ───────────────────────────────────────


class TestFtsHelpers:
    def test_ensure_fts_table_idempotent(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        # Should not raise when called twice
        ensure_fts_table(engine)
        ensure_fts_table(engine)

    def test_insert_and_search_fts(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        ensure_fts_table(engine)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            insert_fts(session, "msg_1", "conv_1", "hello world")
            insert_fts(session, "msg_2", "conv_1", "goodbye world")

        with managed() as session:
            rows = session.execute(
                text(
                    "SELECT item_id FROM conversation_items_fts "
                    "WHERE conversation_items_fts MATCH 'hello'"
                )
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "msg_1"

    def test_delete_fts_by_conversation(self, db_uri: str) -> None:
        engine = get_or_create_engine(db_uri)
        ensure_fts_table(engine)
        managed = make_managed_session_maker(engine)

        with managed() as session:
            insert_fts(session, "msg_a", "conv_del", "searchable text")
            insert_fts(session, "msg_b", "conv_keep", "also searchable")

        with managed() as session:
            delete_fts_by_conversation(session, "conv_del")

        with managed() as session:
            deleted = session.execute(
                text(
                    "SELECT item_id FROM conversation_items_fts WHERE conversation_id = 'conv_del'"
                )
            ).fetchall()
            assert len(deleted) == 0

            kept = session.execute(
                text(
                    "SELECT item_id FROM conversation_items_fts "
                    "WHERE conversation_id = 'conv_keep'"
                )
            ).fetchall()
            assert len(kept) == 1


# ── Timestamp helpers ─────────────────────────────────


class TestTimestampHelpers:
    def test_now_epoch_is_close_to_time(self) -> None:
        before = int(time.time())
        result = now_epoch()
        after = int(time.time())
        assert before <= result <= after

    def test_now_epoch_us_is_microseconds(self) -> None:
        result = now_epoch_us()
        # Should be roughly time.time() * 1_000_000
        expected = int(time.time() * 1_000_000)
        assert abs(result - expected) < 1_000_000  # within 1 second

    def test_now_epoch_us_is_monotonically_increasing(self) -> None:
        """Two consecutive calls should produce non-decreasing values."""
        a = now_epoch_us()
        b = now_epoch_us()
        assert b >= a

    def test_utc_day_known_value(self) -> None:
        # 2026-06-16 00:00:00 UTC = 1781568000
        assert utc_day(1781568000) == "2026-06-16"

    def test_utc_day_midnight_boundary(self) -> None:
        # 1 second before midnight vs midnight
        day_before = utc_day(1781568000 - 1)
        day_at = utc_day(1781568000)
        assert day_before == "2026-06-15"
        assert day_at == "2026-06-16"

    def test_utc_day_format(self) -> None:
        result = utc_day(now_epoch())
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", result)
