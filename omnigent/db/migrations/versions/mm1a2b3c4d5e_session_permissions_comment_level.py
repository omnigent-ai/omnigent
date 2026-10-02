"""Allow the comment-only session permission level (5).

Widens ``ck_session_permissions_level`` to admit ``5``. Downgrade rewrites
comment grants as read grants in bounded primary-key batches, which keeps the
grantee's access but drops commenting, then restores the old constraint.

Deploy this revision only with a build whose permission checks rank levels
(``omnigent.server.auth.level_rank``): numerically, 5 exceeds owner (4), so a
build that compares level integers directly would treat a comment grant as
owner access. Builds without this revision refuse to start against it, so
rolling the application back past it requires downgrading this revision first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "session_permissions"
_CHECK = "ck_session_permissions_level"
_LEVEL_READ = 1
_LEVEL_COMMENT = 5
_BATCH_SIZE = 500

_PERMISSIONS = sa.table(
    _TABLE,
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("user_id", sa.String()),
    sa.column("conversation_id"),
    sa.column("level", sa.Integer()),
)
# Primary-key order, so each batch is a contiguous range of the PK index.
_KEY = (
    _PERMISSIONS.c.workspace_id,
    _PERMISSIONS.c.user_id,
    _PERMISSIONS.c.conversation_id,
)


def _publish(bind: sa.Connection) -> None:
    if bind.dialect.name == "cockroachdb":
        # Schema changes must be visible before subsequent reads and writes.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _commit_batch(bind: sa.Connection) -> None:
    if bind.dialect.name == "cockroachdb":
        _publish(bind)
    else:
        with op.get_context().autocommit_block():
            pass


def _after(bind: sa.Connection, cursor: Sequence[Any]) -> Any:
    if bind.dialect.name == "mysql":
        # MySQL treats tuple inequalities as filters over a full index scan, so
        # expand the comparison into a PRIMARY range MySQL can seek.
        expression = _KEY[-1] > cursor[-1]
        for column, value in zip(reversed(_KEY[:-1]), reversed(cursor[:-1]), strict=True):
            expression = sa.or_(column > value, sa.and_(column == value, expression))
        return expression
    # Elsewhere the row-value comparison is the index condition; the expanded
    # form makes PostgreSQL filter every earlier row.
    return sa.tuple_(*_KEY) > tuple(cursor)


def _key_of(row: sa.Row[Any]) -> tuple[Any, ...]:
    conversation_id = row.conversation_id
    if not isinstance(conversation_id, str | bytes):
        conversation_id = bytes(conversation_id)
    return (row.workspace_id, row.user_id, conversation_id)


_DEMOTE_ONE = (
    _PERMISSIONS.update()
    .where(
        _PERMISSIONS.c.workspace_id == sa.bindparam("key_workspace_id"),
        _PERMISSIONS.c.user_id == sa.bindparam("key_user_id"),
        _PERMISSIONS.c.conversation_id == sa.bindparam("key_conversation_id"),
        _PERMISSIONS.c.level == _LEVEL_COMMENT,
    )
    .values(level=_LEVEL_READ)
)


def _demote_comment_grants(bind: sa.Connection) -> None:
    cursor: tuple[Any, ...] | None = None
    while True:
        query = sa.select(*_KEY, _PERMISSIONS.c.level).order_by(*_KEY).limit(_BATCH_SIZE)
        if cursor is not None:
            query = query.where(_after(bind, cursor))
        rows = bind.execute(query).all()
        if not rows:
            return
        # Update by complete primary key so each write is a point lookup.
        demoted = [
            dict(
                zip(
                    ("key_workspace_id", "key_user_id", "key_conversation_id"),
                    _key_of(row),
                    strict=True,
                )
            )
            for row in rows
            if row.level == _LEVEL_COMMENT
        ]
        if demoted:
            bind.execute(_DEMOTE_ONE, demoted)
        cursor = _key_of(rows[-1])
        _commit_batch(bind)


def _replace_check(sqltext: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch_op:
            batch_op.drop_constraint(_CHECK, type_="check")
            batch_op.create_check_constraint(_CHECK, sqltext)
        return
    # MySQL DDL is not transactional, so a rerun may find the constraint dropped.
    existing = {check["name"] for check in sa.inspect(bind).get_check_constraints(_TABLE)}
    if _CHECK in existing:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_constraint(_CHECK, type_="check")
        # CockroachDB publishes the drop at commit; re-adding the name first collides.
        _publish(bind)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.create_check_constraint(_CHECK, sqltext)
    _publish(bind)


def upgrade() -> None:
    if op.get_context().as_sql:
        # The rerun-safe drop inspects live constraints, which offline SQL can't do.
        raise RuntimeError("Admitting the comment level requires an online migration")
    _replace_check("level IN (1, 2, 3, 4, 5)")


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Demoting comment grants requires an online migration")
    _demote_comment_grants(op.get_bind())
    _replace_check("level IN (1, 2, 3, 4)")
