"""Keyset-pagination predicates that every supported database can seek on."""

from __future__ import annotations

from operator import ge, gt, le, lt
from typing import Any

from sqlalchemy import and_, or_, tuple_
from sqlalchemy.sql.elements import ColumnElement


def keyset_after(
    dialect_name: str,
    columns: tuple[Any, ...],
    cursor: tuple[Any, ...],
    *,
    inclusive: bool = False,
    descending: bool = False,
) -> ColumnElement[bool]:
    """
    Select the rows that sort after *cursor* on *columns*.

    SQLite and PostgreSQL seek an index on a row-value comparison; MySQL needs
    it spelled out as scalar ranges to keep a page's scan ``O(limit)``.

    :param dialect_name: The engine's dialect, e.g. ``"mysql"``.
    :param columns: The sort key, e.g. ``(SqlAgent.created_at, SqlAgent.id)``.
    :param cursor: The last row's key values, e.g. ``(1700000000, "agent_id")``.
    :param inclusive: Whether to include the cursor row itself.
    :param descending: Whether the walk runs in descending order.
    :returns: The predicate to add to the query's ``WHERE``.
    """
    compare = lt if descending else gt
    compare_inclusive = le if descending else ge
    if dialect_name != "mysql":
        return (compare_inclusive if inclusive else compare)(tuple_(*columns), cursor)
    return or_(
        *(
            and_(
                *(columns[previous] == cursor[previous] for previous in range(index)),
                (compare_inclusive if inclusive and index == len(columns) - 1 else compare)(
                    column, cursor[index]
                ),
            )
            for index, column in enumerate(columns)
        )
    )
