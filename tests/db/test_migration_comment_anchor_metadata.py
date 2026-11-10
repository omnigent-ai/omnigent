"""The optional comment anchor column preserves legacy rows and writers."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.compression import CompressedText
from omnigent.db.utils import _build_alembic_config


def test_comment_anchor_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'comment-anchors.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "1ad1db53f99d")
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
        anchor = sa.column("anchor_metadata", CompressedText(max_stored_bytes=1_024))
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

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "1ad1db53f99d")

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
