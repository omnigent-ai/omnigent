"""Index original user agents by exact owner/name identity.

Apply before the identity-aware writer release, with agent writes quiesced until
that release is active. Old writers ignore the nullable column; their subsequent
writes need another backfill before enabling identity queries. Reads remain
compatible. Roll back application code before downgrading this schema.
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY

revision = "e1a2b3c4d5f6"
down_revision = "d0a1f879a01d"
branch_labels = None
depends_on = None

_TABLE = "agents"
_COLUMN = "user_identity"
_INDEX = "ix_agents_user_identity"
_COLUMNS = ["workspace_id", _COLUMN, "created_at", "id"]
_BATCH_SIZE = 100
_BINARY = sa.LargeBinary(16).with_variant(BINARY(16), "mysql")


def _commit_batch(bind: sa.Connection) -> None:
    if bind.dialect.name == "cockroachdb":
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    else:
        with op.get_context().autocommit_block():
            pass


def _backfill() -> None:
    bind = op.get_bind()
    table = sa.table(
        _TABLE,
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", _BINARY),
        sa.column("kind", sa.SmallInteger()),
        sa.column("name", sa.String(256)),
        sa.column("created_by", sa.String(128)),
        sa.column("bundle_location", sa.String(512)),
        sa.column(_COLUMN, _BINARY),
    )
    cursor: tuple[int, bytes] | None = None
    while True:
        query = sa.select(table).order_by(table.c.workspace_id, table.c.id)
        if cursor is not None:
            query = query.where(
                sa.or_(
                    table.c.workspace_id > cursor[0],
                    sa.and_(table.c.workspace_id == cursor[0], table.c.id > cursor[1]),
                )
            )
        rows = bind.execute(query.limit(_BATCH_SIZE)).all()
        if not rows:
            break
        for row in rows:
            agent_id = bytes(row.id).hex()
            original = row.kind == 2 and row.bundle_location.startswith(
                (f"{agent_id}/", f"ag_{agent_id}/")
            )
            identity = (
                hashlib.sha256(f"installed:{row.created_by or ''}\0{row.name}".encode()).digest()[
                    :16
                ]
                if original
                else None
            )
            if row.user_identity != identity:
                bind.execute(
                    table.update()
                    .where(table.c.workspace_id == row.workspace_id, table.c.id == row.id)
                    .values(user_identity=identity)
                )
        cursor = (rows[-1].workspace_id, bytes(rows[-1].id))
        _commit_batch(bind)


def upgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, _BINARY, nullable=True))
        _commit_batch(bind)
    _backfill()
    if bind.dialect.name != "postgresql":
        if _INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE)}:
            op.create_index(_INDEX, _TABLE, _COLUMNS)
        return
    with op.get_context().autocommit_block():
        invalid = bind.execute(
            sa.text(
                "SELECT i.indexrelid::regclass::text FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = to_regclass(:table) AND c.relname = :name "
                "AND NOT i.indisvalid"
            ),
            {"table": _TABLE, "name": _INDEX},
        ).scalar()
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY {invalid}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON {_TABLE} ({', '.join(_COLUMNS)})"
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
