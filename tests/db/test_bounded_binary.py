"""Tests for the byte-limited binary column type."""

from __future__ import annotations

import array

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite

from omnigent.db.bounded_binary import BoundedBinary


def test_values_up_to_the_limit_round_trip_and_larger_ones_are_rejected() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "t",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("value", BoundedBinary(8)),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            table.insert(),
            [
                {"id": 1, "value": b"\x00" * 8},
                {"id": 2, "value": None},
                # Two 4-byte elements: 2 elements, 8 bytes.
                {"id": 3, "value": memoryview(array.array("I", [0] * 2))},
            ],
        )
        rows = conn.execute(sa.select(table.c.value).order_by(table.c.id)).scalars().all()
    assert rows == [b"\x00" * 8, None, b"\x00" * 8]

    with pytest.raises(sa.exc.StatementError) as caught, engine.begin() as conn:
        conn.execute(table.insert(), {"id": 4, "value": b"\x00" * 9})
    assert isinstance(caught.value.orig, ValueError)
    # Four 4-byte elements: 4 elements, 16 bytes.
    with pytest.raises(sa.exc.StatementError) as caught, engine.begin() as conn:
        conn.execute(table.insert(), {"id": 5, "value": memoryview(array.array("I", [0] * 4))})
    assert isinstance(caught.value.orig, ValueError)
    with pytest.raises(sa.exc.StatementError) as caught, engine.begin() as conn:
        conn.execute(table.insert(), {"id": 6, "value": "text"})
    assert isinstance(caught.value.orig, TypeError)


def test_mysql_gets_a_bounded_varbinary_and_other_dialects_a_blob() -> None:
    column = BoundedBinary(16_383)

    assert column.compile(dialect=mysql.dialect()) == "VARBINARY(16383)"
    assert column.compile(dialect=postgresql.dialect()) == "BYTEA"
    assert column.compile(dialect=sqlite.dialect()) == "BLOB"
