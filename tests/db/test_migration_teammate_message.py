"""Upgrade and rollback checks for the teammate-message item constraint."""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


def _allows_code_12(engine: sa.Engine) -> bool:
    checks = sa.inspect(engine).get_check_constraints("conversation_items")
    constraint = next(row for row in checks if row["name"] == "ck_conversation_items_type")
    return re.search(r"\b12\b", constraint["sqltext"]) is not None


def test_teammate_item_check_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'teammate.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)

    for revision, allows_code_12 in (
        ("head", True),
        ("ll1a2b3c4d5e", False),
        ("head", True),
    ):
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            if revision == "ll1a2b3c4d5e":
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)
        assert _allows_code_12(engine) is allows_code_12

    engine.dispose()


def _count_type_12(engine: sa.Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT COUNT(*) FROM conversation_items WHERE type = 12")
        ).scalar_one()


def test_teammate_item_downgrade_drops_stored_type_12_rows(tmp_path: Path) -> None:
    """
    Downgrade succeeds with a stored teammate row by dropping it first.

    A naive downgrade that only narrows the CHECK would fail on an
    existing ``type = 12`` row. The migration deletes those rows first,
    so rollback stays reversible (and leaves nothing for an older
    decoder to choke on).
    """
    uri = f"sqlite:///{tmp_path / 'teammate_rollback.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    # Seed one teammate_message row (FKs off so we needn't build a
    # parent conversation; UUID columns take raw 16-byte blobs).
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            sa.text(
                "INSERT INTO conversation_items "
                "(workspace_id, conversation_id, id, response_id, created_at, "
                " status, position, type, data, search_text) "
                "VALUES (0, :cid, :iid, 'resp_x', 1, 1, 0, 12, :data, '')"
            ),
            {
                "cid": b"\x00" * 16,
                "iid": b"\x11" * 16,
                "data": '{"teammate_id": "buddy", "text": "hi"}',
            },
        )
    assert _count_type_12(engine) == 1

    # Without the row-drop this downgrade would raise on the CHECK.
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "ll1a2b3c4d5e")

    assert _count_type_12(engine) == 0
    assert _allows_code_12(engine) is False

    engine.dispose()
