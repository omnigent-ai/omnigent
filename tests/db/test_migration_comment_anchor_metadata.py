"""The optional comment anchor column preserves legacy rows and writers."""

import io
from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.compression import CompressedText
from omnigent.db.utils import _build_alembic_config


def test_comment_anchor_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'comment-anchors.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "c7cd88f24c1c")
        comments = sa.Table("comments", sa.MetaData(), autoload_with=engine)
        legacy = {
            "workspace_id": 0,
            "id": bytes.fromhex("0123456789abcdef0123456789abcdef"),
            "conversation_id": bytes.fromhex("fedcba9876543210fedcba9876543210"),
            "path": "main.py",
            "start_index": 2,
            "end_index": 8,
            "body": b"Keep this check",
            "status": 1,
            "created_at": 100,
            "updated_at": 100_000_000,
        }
        with engine.begin() as connection:
            connection.execute(comments.insert().values(**legacy))
            config.attributes["connection"] = connection
            command.upgrade(config, "f9b3e6c2a401")

        column = next(
            column
            for column in sa.inspect(engine).get_columns("comments")
            if column["name"] == "anchor_metadata"
        )
        assert column["nullable"] is True
        assert column["default"] is None
        anchor = sa.column("anchor_metadata", CompressedText(max_stored_bytes=16_383))
        with engine.begin() as connection:
            assert dict(connection.execute(comments.select()).mappings().one()) == {
                **legacy,
                "anchor_content": None,
                "created_by": None,
            }
            assert connection.execute(sa.select(anchor).select_from(comments)).scalar_one() is None
            # An existing writer can still omit the new column after upgrade.
            connection.execute(comments.insert().values(**{**legacy, "id": b"x" * 16}))
            assert connection.execute(sa.select(anchor).select_from(comments)).scalars().all() == [
                None,
                None,
            ]
            stored_anchor = (
                '{"pr_url":"https://example.com/' + "path/" * 100 + '","side":"LEFT","line":9}'
            )
            connection.execute(
                sa.update(sa.table("comments", anchor)).values(anchor_metadata=stored_anchor)
            )
            assert connection.execute(sa.select(anchor).select_from(comments)).scalars().all() == [
                stored_anchor,
                stored_anchor,
            ]

        # Bypass the codec: the database must bound both blobs and SQLite TEXT.
        reflected = sa.Table("comments", sa.MetaData(), autoload_with=engine)
        for value in (b"x" * 16_383, "é" * 8_191 + "x"):
            with engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE comments SET anchor_metadata = :value"), {"value": value}
                )
        with engine.begin() as connection:
            connection.execute(reflected.update().values(anchor_metadata=b"x" * 16_383))
        for value in (b"x" * 16_384, "é" * 8_192, "\x00" + "x" * 16_383):
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE comments SET anchor_metadata = :value"), {"value": value}
                )
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
            connection.execute(reflected.update().values(anchor_metadata=b"x" * 16_384))

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "c7cd88f24c1c")

        assert "anchor_metadata" not in {
            column["name"] for column in sa.inspect(engine).get_columns("comments")
        }
        with engine.connect() as connection:
            assert connection.execute(sa.select(comments.c.body)).scalars().all() == [
                legacy["body"],
                legacy["body"],
            ]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "dialect_name", ["sqlite", "cloudflare_d1", "postgresql", "mysql", "cockroachdb"]
)
def test_comment_anchor_migration_byte_constraint_sql(dialect_name: str) -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name={"cloudflare_d1": "sqlite", "cockroachdb": "postgresql"}.get(
            dialect_name, dialect_name
        ),
        opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
    )
    context.dialect.name = dialect_name
    migration = import_module(
        "omnigent.db.migrations.versions.f9b3e6c2a401_add_comment_anchor_metadata"
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "ck_comments_anchor_metadata_bytes" in sql
    assert "anchor_metadata IS NULL" in sql
    assert "<= 16383" in sql
    if dialect_name in {"sqlite", "cloudflare_d1"}:
        assert "length(CAST(anchor_metadata AS BLOB))" in sql
        assert sql.count("ALTER TABLE") == 1
    else:
        assert "octet_length(anchor_metadata)" in sql
        output.truncate(0)
        output.seek(0)
        with Operations.context(context):
            migration.downgrade()
        sql = output.getvalue()
        assert sql.index("ck_comments_anchor_metadata_bytes") < sql.index(
            "DROP COLUMN anchor_metadata"
        )
