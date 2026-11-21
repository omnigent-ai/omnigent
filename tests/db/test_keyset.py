"""The keyset predicate each database can seek on."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select
from sqlalchemy.dialects import mysql, sqlite

from omnigent.db.keyset import keyset_after

_ROWS = Table("listing", MetaData(), Column("created_at", Integer), Column("id", String))


def _where(dialect_name: str, **options: bool) -> str:
    columns = (_ROWS.c.created_at, _ROWS.c.id)
    predicate = keyset_after(dialect_name, columns, (5, "b"), **options)
    dialect = mysql.dialect() if dialect_name == "mysql" else sqlite.dialect()
    compiled = (
        select(_ROWS.c.id)
        .where(predicate)
        .compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    )
    return str(compiled).split("WHERE ", 1)[1]


@pytest.mark.parametrize(
    ("options", "sqlite_where", "mysql_where"),
    [
        (
            {},
            "(listing.created_at, listing.id) > (5, 'b')",
            "listing.created_at > 5 OR listing.created_at = 5 AND listing.id > 'b'",
        ),
        (
            {"descending": True},
            "(listing.created_at, listing.id) < (5, 'b')",
            "listing.created_at < 5 OR listing.created_at = 5 AND listing.id < 'b'",
        ),
        (
            {"inclusive": True},
            "(listing.created_at, listing.id) >= (5, 'b')",
            "listing.created_at > 5 OR listing.created_at = 5 AND listing.id >= 'b'",
        ),
    ],
    ids=["after", "descending", "inclusive"],
)
def test_mysql_gets_scalar_ranges_and_others_a_row_value(
    options: dict[str, bool], sqlite_where: str, mysql_where: str
) -> None:
    """MySQL can't seek a row-value comparison, so it gets the same order as scalar ranges."""
    assert _where("sqlite", **options) == sqlite_where
    assert _where("mysql", **options) == mysql_where
