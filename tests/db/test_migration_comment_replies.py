"""Reply schema bounds apply to migrations and metadata-created databases."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import SqlCommentReply
from omnigent.db.envelope import EnvelopedText, seal_text
from omnigent.db.utils import _build_alembic_config


@pytest.mark.parametrize("source", ["migration", "model"])
def test_comment_reply_schema(tmp_path: Path, source: str) -> None:
    uri = f"sqlite:///{tmp_path / 'replies.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        if source == "migration":
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "4d8e2b7f1a63")
        else:
            SqlCommentReply.__table__.create(engine)
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint("comment_replies")["constrained_columns"] == [
            "workspace_id",
            "conversation_id",
            "comment_id",
            "id",
        ]
        assert not inspector.get_foreign_keys("comment_replies")
        assert all(
            column["default"] is None for column in inspector.get_columns("comment_replies")
        )
        index = inspector.get_indexes("comment_replies")[0]
        assert index["name"] == "ix_comment_replies_thread"
        assert index["column_names"] == [
            "workspace_id",
            "conversation_id",
            "comment_id",
            "created_at",
            "id",
        ]
        table = sa.Table("comment_replies", sa.MetaData(), autoload_with=engine)
        row = {
            "workspace_id": 0,
            "conversation_id": b"c" * 16,
            "comment_id": b"p" * 16,
            "id": b"r" * 16,
            "author_kind": 1,
            "body": seal_text("Question?"),
            "created_by": None,
            "created_at": 1234567890123456,
        }
        with engine.begin() as connection:
            connection.execute(table.insert().values(**row))
            body = sa.column("body", EnvelopedText(16_383))
            assert (
                connection.execute(sa.select(body).select_from(table)).scalar_one() == "Question?"
            )
        # Raw writes must obey SQLite's otherwise unenforced types and byte bounds.
        invalid = [
            ("workspace_id", "tenant"),
            ("workspace_id", 1.5),
            ("conversation_id", b"short"),
            ("comment_id", b"short"),
            ("id", b"short"),
            ("author_kind", 3),
            ("body", b"x" * 16_384),
            ("body", "text"),
            ("created_by", "é" * 257),
            ("created_at", "yesterday"),
        ]
        for field, value in invalid:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO comment_replies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple({**row, "id": b"s" * 16, field: value}.values()),
                )
        if source == "migration":
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                command.downgrade(config, "ffeec66eadb5")
            assert "comment_replies" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
