"""Reserve atomic session-status ownership and error state.

Deploy before the application consumers. Existing rows retain NULL and legacy
status/error behavior. Recovery must remain disabled until every writer replaces
ownership on a status/error/binding change. Roll application code back with these
columns retained; downgrade is permitted only while both columns remain unused.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

from omnigent.db.db_models import Uuid16

revision: str = "d3a9e7b1c402"
down_revision: str | None = "c8e4f2a6b901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "omnigent_conversation_metadata"
_CHECKS = {
    "ck_conv_meta_live_version_size": (
        "live_status_version IS NULL OR length(live_status_version) = 16"
    ),
    "ck_conv_meta_live_error_size": (
        "live_error_state IS NULL OR length(live_error_state) <= 16383"
    ),
    "ck_conv_meta_live_error_version": (
        "live_error_state IS NULL OR live_status_version IS NOT NULL"
    ),
}


def upgrade() -> None:
    """Add nullable storage without changing current statuses or error labels."""
    columns: set[str] = set()
    checks: set[str | None] = set()
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns(_TABLE)}
        checks = {check["name"] for check in inspector.get_check_constraints(_TABLE)}
    with op.batch_alter_table(_TABLE) as batch:
        # Resume a migration after nontransactional DDL committed a subset.
        if "live_status_version" not in columns:
            batch.add_column(sa.Column("live_status_version", Uuid16(), nullable=True))
        if "live_error_state" not in columns:
            batch.add_column(sa.Column("live_error_state", sa.LargeBinary(16383), nullable=True))
        for name, expression in _CHECKS.items():
            if name not in checks:
                batch.create_check_constraint(name, expression)


def downgrade() -> None:
    """Remove unused storage; preserve any state written by a later application."""
    if context.is_offline_mode():
        raise RuntimeError("Live failure ownership downgrade requires a live connection")
    table = sa.table(
        _TABLE,
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("live_status_version", Uuid16()),
    )
    cursor: tuple[int, str] | None = None
    while True:
        query = sa.select(table).order_by(table.c.workspace_id, table.c.id).limit(100)
        if cursor is not None:
            if op.get_bind().dialect.name == "mysql":
                # MySQL needs expanded inequalities; SQLite/Postgres seek tuples.
                query = query.where(
                    sa.or_(
                        table.c.workspace_id > cursor[0],
                        sa.and_(table.c.workspace_id == cursor[0], table.c.id > cursor[1]),
                    )
                )
            else:
                query = query.where(sa.tuple_(table.c.workspace_id, table.c.id) > cursor)
        rows = op.get_bind().execute(query).all()
        if not rows:
            break
        if any(row.live_status_version is not None for row in rows):
            raise RuntimeError(
                "Live failure ownership has been used; retain its columns during rollback"
            )
        cursor = (rows[-1].workspace_id, rows[-1].id)
    with op.batch_alter_table(_TABLE) as batch:
        for name in reversed(_CHECKS):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("live_error_state")
        batch.drop_column("live_status_version")
