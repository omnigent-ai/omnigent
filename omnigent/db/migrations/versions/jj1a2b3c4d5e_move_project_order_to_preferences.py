"""Move personal project ordering into the preferences table."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from sqlalchemy.sql import Executable

revision: str = "jj1a2b3c4d5e"
down_revision: str | None = "ii1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)
_BINARY = sa.LargeBinary().with_variant(MEDIUMBLOB(), "mysql")
_USERS = sa.table(
    "users",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("id", sa.String(128)),
    sa.column("is_admin", sa.Boolean()),
    sa.column("project_order", _BINARY),
)
_PREFERENCES = sa.table(
    "preferences",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("user_id", sa.String(128)),
    sa.column("key", sa.String(128)),
    sa.column("value", _BINARY),
)


def _begin_sqlite_transaction(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite" and not getattr(
        bind.connection.driver_connection, "in_transaction", False
    ):
        # Legacy sqlite3 transaction control otherwise commits DDL and outermost savepoints.
        bind.exec_driver_sql("BEGIN")


def _publish_crdb_changes(bind: sa.Connection) -> None:
    if bind.dialect.name == "cockroachdb":
        # Publish schema changes before copying, and durable copies before dropping their source.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _copy_with_retries(
    bind: sa.Connection, statements: Sequence[Executable], *, operation: str
) -> None:
    is_crdb = bind.dialect.name == "cockroachdb"
    for attempt in range(3):
        try:
            if is_crdb:
                # Serialization failures require a new transaction, including failures at commit.
                if not bind.in_transaction():
                    bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                for statement in statements:
                    bind.execute(statement)
                bind.commit()
            else:
                # PostgreSQL needs rollback to the savepoint before another statement can run.
                with bind.begin_nested():
                    for statement in statements:
                        bind.execute(statement)
            return
        except sa.exc.SQLAlchemyError:
            if is_crdb:
                bind.rollback()
            if attempt == 2:
                _logger.warning(
                    "Could not %s project order preferences after 3 attempts; "
                    "continuing with source removal and default preferences",
                    operation,
                    exc_info=True,
                )
            else:
                time.sleep(0.1 * (2**attempt))


def _project_order_column_exists(bind: sa.Connection) -> bool:
    return "project_order" in {column["name"] for column in sa.inspect(bind).get_columns("users")}


def _copy_to_preferences() -> Executable:
    source = _USERS.outerjoin(
        _PREFERENCES,
        sa.and_(
            _PREFERENCES.c.workspace_id == _USERS.c.workspace_id,
            _PREFERENCES.c.user_id == _USERS.c.id,
            _PREFERENCES.c.key == "project_order",
        ),
    )
    return _PREFERENCES.insert().from_select(
        ["workspace_id", "user_id", "key", "value"],
        sa.select(
            _USERS.c.workspace_id,
            _USERS.c.id,
            sa.literal("project_order"),
            _USERS.c.project_order,
        )
        .select_from(source)
        .where(_USERS.c.project_order.is_not(None), _PREFERENCES.c.user_id.is_(None)),
    )


def upgrade() -> None:
    bind = op.get_bind()
    _begin_sqlite_transaction(bind)
    if not sa.inspect(bind).has_table("preferences"):
        op.create_table(
            "preferences",
            sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("key", sa.String(128), nullable=False),
            sa.Column("value", _BINARY, nullable=False),
            sa.PrimaryKeyConstraint("workspace_id", "user_id", "key"),
        )
    _publish_crdb_changes(bind)
    if not _project_order_column_exists(bind):
        return
    _copy_with_retries(bind, [_copy_to_preferences()], operation="migrate")
    _publish_crdb_changes(bind)
    with op.batch_alter_table("users") as batch:
        batch.drop_column("project_order")


def _restore_project_orders() -> Sequence[Executable]:
    source = _PREFERENCES.outerjoin(
        _USERS,
        sa.and_(
            _USERS.c.workspace_id == _PREFERENCES.c.workspace_id,
            _USERS.c.id == _PREFERENCES.c.user_id,
        ),
    )
    missing_users = _USERS.insert().from_select(
        ["workspace_id", "id", "is_admin", "project_order"],
        sa.select(
            _PREFERENCES.c.workspace_id,
            _PREFERENCES.c.user_id,
            sa.false(),
            _PREFERENCES.c.value,
        )
        .select_from(source)
        .where(_PREFERENCES.c.key == "project_order", _USERS.c.id.is_(None)),
    )
    saved_order = sa.select(_PREFERENCES.c.value).where(
        _PREFERENCES.c.workspace_id == _USERS.c.workspace_id,
        _PREFERENCES.c.user_id == _USERS.c.id,
        _PREFERENCES.c.key == "project_order",
    )
    existing_users = (
        _USERS.update()
        .where(_USERS.c.project_order.is_(None), saved_order.exists())
        .values(project_order=saved_order.scalar_subquery())
    )
    return [missing_users, existing_users]


def downgrade() -> None:
    bind = op.get_bind()
    _begin_sqlite_transaction(bind)
    if not _project_order_column_exists(bind):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("project_order", _BINARY, nullable=True))
    _publish_crdb_changes(bind)
    if not sa.inspect(bind).has_table("preferences"):
        return
    _copy_with_retries(bind, _restore_project_orders(), operation="restore")
    _publish_crdb_changes(bind)
    op.drop_table("preferences")
