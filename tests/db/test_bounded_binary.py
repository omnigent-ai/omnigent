"""Tests for the byte-limited binary column type."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

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
        conn.execute(table.insert(), [{"id": 1, "value": b"\x00" * 8}, {"id": 2, "value": None}])
        rows = conn.execute(sa.select(table.c.value).order_by(table.c.id)).scalars().all()
    assert rows == [b"\x00" * 8, None]

    with pytest.raises(sa.exc.StatementError) as caught, engine.begin() as conn:
        conn.execute(table.insert(), {"id": 3, "value": b"\x00" * 9})
    assert isinstance(caught.value.orig, ValueError)
