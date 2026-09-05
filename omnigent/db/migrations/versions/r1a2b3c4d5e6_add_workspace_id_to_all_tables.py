"""Add workspace_id to every table and fold it into the primary key.

Revision ID: r1a2b3c4d5e6
Revises: q1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Adds a ``workspace_id`` tenant-partition column to all twelve tables and
extends each primary key to ``(workspace_id, <existing pk cols>)``.  The
column is NOT NULL with ``server_default = 0`` so existing rows backfill
to workspace 0 (the single-workspace / unassigned sentinel) and inserts
that omit it land in workspace 0.  ``workspace_id`` leads the composite
key so rows for one workspace stay contiguous for prefix scans.

``p1a2b3c4d5e6`` removed omnigent's own FK constraints, but an FK from an
operator-added table may still reference a widened table's PK. On PostgreSQL
such FKs are re-bound onto a parallel UNIQUE constraint on the referenced
columns (never dropped), so the PK rebuild below stays a local operation per
table while the operator's referential integrity survives.

SQLite note: ``batch_alter_table(recreate="always")`` rebuilds the table
so the primary key can change (SQLite cannot alter a PK in place); the
new ``create_primary_key`` overrides the reflected single-column PK.  On
PostgreSQL the existing named PK is dropped explicitly first (a table can
hold only one primary key) before the wider one is added.  Both paths
guard the rebuilds with ``PRAGMA foreign_keys`` on SQLite.
"""

from __future__ import annotations

import contextlib
import hashlib
import warnings
from collections.abc import Iterator, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r1a2b3c4d5e6"
down_revision: str | None = "q1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table mapped to the primary-key columns it had before this
# migration. The new primary key is ``["workspace_id", *existing]``.
_TABLE_PKS: dict[str, list[str]] = {
    "agents": ["id"],
    "files": ["id"],
    "users": ["id"],
    "account_tokens": ["id"],
    "session_permissions": ["user_id", "conversation_id"],
    "conversations": ["id"],
    "conversation_items": ["id"],
    "conversation_labels": ["conversation_id", "key"],
    "comments": ["id"],
    "policies": ["id"],
    "hosts": ["owner", "name"],
    "user_daily_cost": ["user_id", "day_utc"],
}


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _existing_pk_name(table: str) -> str | None:
    """Reflect the current primary-key constraint name (PostgreSQL path)."""
    return sa.inspect(op.get_bind()).get_pk_constraint(table).get("name")


def _detach_fks_bound_to_widened_pks(tables: Sequence[str]) -> list[tuple[str, str, str]]:
    """Move FKs off the primary keys this migration drops (PostgreSQL).

    A PK cannot be dropped while a foreign key is bound to its index, and an
    FK from an operator-added table (e.g. ``host_permissions_user_id_fkey``
    referencing ``users.id``) survives ``p1a2b3c4d5e6`` — PostgreSQL then
    refuses ``DROP CONSTRAINT <pk>`` with ``DependentObjectsStillExist``,
    crash-looping every server boot. Dropping such FKs would silently destroy
    the operator's referential integrity, so instead: ensure the referenced
    columns carry a parallel UNIQUE constraint, detach the FK here, and let
    ``upgrade`` re-attach it once the composite PK is in place — the FK then
    binds to the UNIQUE index and stays enforced. Everything runs inside the
    migration's transaction, so the constraint never lapses observably.

    Returns the detached FKs as ``(child_table, name, definition)``.
    """
    bind = op.get_bind()
    fks = (
        bind.execute(
            sa.text(
                "SELECT con.conname AS name,"
                " con.conrelid::regclass::text AS child,"
                " ref.relname AS parent,"
                " pg_get_constraintdef(con.oid) AS definition,"
                " (SELECT string_agg(quote_ident(att.attname), ', ' ORDER BY ord.n)"
                "    FROM unnest(con.confkey) WITH ORDINALITY AS ord(attnum, n)"
                "    JOIN pg_attribute att ON att.attrelid = con.confrelid"
                "     AND att.attnum = ord.attnum) AS ref_cols,"
                " array_to_string(con.confkey, ',') AS ref_cols_key,"
                " (SELECT string_agg(att.attname, '_' ORDER BY ord.n)"
                "    FROM unnest(con.confkey) WITH ORDINALITY AS ord(attnum, n)"
                "    JOIN pg_attribute att ON att.attrelid = con.confrelid"
                "     AND att.attnum = ord.attnum) AS ref_cols_label"
                " FROM pg_constraint con"
                " JOIN pg_class ref ON ref.oid = con.confrelid"
                " JOIN pg_namespace nsp ON nsp.oid = ref.relnamespace"
                " JOIN pg_index idx ON idx.indexrelid = con.conindid"
                " WHERE con.contype = 'f' AND idx.indisprimary"
                " AND nsp.nspname = current_schema()"
                " AND ref.relname = ANY(:tables)"
                " ORDER BY con.conname"
            ).bindparams(sa.bindparam("tables", list(tables), type_=sa.ARRAY(sa.Text)))
        )
        .mappings()
        .all()
    )
    # One parallel UNIQUE per distinct referenced column set, unless an
    # equivalent unique constraint already exists on those exact columns.
    # The dedup key is the ordered attnum list, which — unlike joined column
    # names — cannot alias two distinct column sets.
    needed: dict[tuple[str, str], tuple[str, str]] = {}
    for fk in fks:
        needed[(fk["parent"], fk["ref_cols_key"])] = (fk["ref_cols"], fk["ref_cols_label"])
    for (parent, cols_key), (ref_cols, cols_label) in needed.items():
        existing = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint u"
                " JOIN pg_class rel ON rel.oid = u.conrelid"
                " JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace"
                " WHERE u.contype = 'u'"
                " AND nsp.nspname = current_schema()"
                " AND rel.relname = :parent"
                " AND array_to_string(u.conkey, ',') = :cols_key"
            ),
            {"parent": parent, "cols_key": cols_key},
        ).first()
        if existing is None:
            # Stay under PostgreSQL's 63-byte identifier limit, and fall back
            # to a digest when the readable name is taken (two distinct column
            # sets can share an underscore-joined label).
            uq_name = f"uq_{parent}_{cols_label}"
            taken = bind.execute(
                sa.text(
                    "SELECT 1 FROM pg_constraint c"
                    " JOIN pg_class rel ON rel.oid = c.conrelid"
                    " JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace"
                    " WHERE nsp.nspname = current_schema() AND c.conname = :name"
                ),
                {"name": uq_name},
            ).first()
            if len(uq_name) > 63 or taken is not None:
                digest = hashlib.sha256(f"{parent}:{cols_key}".encode()).hexdigest()[:10]
                uq_name = f"uq_{parent[:44]}_{digest}"
            op.execute(
                sa.text(f'ALTER TABLE "{parent}" ADD CONSTRAINT "{uq_name}" UNIQUE ({ref_cols})')
            )
    detached: list[tuple[str, str, str]] = []
    for fk in fks:
        op.execute(sa.text(f'ALTER TABLE {fk["child"]} DROP CONSTRAINT "{fk["name"]}"'))
        detached.append((fk["child"], fk["name"], fk["definition"]))
    return detached


@contextlib.contextmanager
def _quiet_pk_override() -> Iterator[None]:
    """
    Silence the expected SQLite batch-rebuild warning about the reflected
    single-column PK not matching the wider one we install. The override is
    intentional here, and this fires once per table on every fresh DB.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*not matching locally specified columns.*",
            category=sa.exc.SAWarning,
        )
        yield


def upgrade() -> None:
    """Add ``workspace_id`` and widen every primary key to include it."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    is_mysql = op.get_bind().dialect.name == "mysql"
    is_postgres = not sqlite and not is_mysql
    # PostgreSQL refuses to drop a PK while an FK is bound to its index;
    # detach surviving FKs now and re-attach them after the widening.
    detached_fks = _detach_fks_bound_to_widened_pks(list(_TABLE_PKS)) if is_postgres else []
    for table, pk_cols in _TABLE_PKS.items():
        if is_mysql:
            # Use raw DDL on MySQL to avoid batch_alter_table reading ORM
            # metadata and trying to apply server_defaults (e.g. '' on title)
            # that MySQL rejects on TEXT/BLOB columns.
            pk_col_list = ", ".join(f"`{c}`" for c in ["workspace_id", *pk_cols])
            op.execute(
                sa.text(
                    f"ALTER TABLE `{table}` "
                    f"ADD COLUMN workspace_id BIGINT NOT NULL DEFAULT 0 FIRST, "
                    f"DROP PRIMARY KEY, "
                    f"ADD CONSTRAINT `pk_{table}` PRIMARY KEY ({pk_col_list})"
                )
            )
            continue
        # On PostgreSQL the current PK must be dropped before a wider one
        # can be added; on SQLite the batch rebuild overrides it in place.
        old_pk_name = None if sqlite else _existing_pk_name(table)
        with (
            _quiet_pk_override(),
            op.batch_alter_table(table, recreate="always" if sqlite else "auto") as batch_op,
        ):
            batch_op.add_column(
                sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")
            )
            if old_pk_name is not None:
                batch_op.drop_constraint(old_pk_name, type_="primary")
            batch_op.create_primary_key(f"pk_{table}", ["workspace_id", *pk_cols])

    # Re-attach the detached FKs; each now binds to the parallel UNIQUE
    # index on its referenced columns (the composite PK no longer matches).
    for child, name, definition in detached_fks:
        op.execute(sa.text(f'ALTER TABLE {child} ADD CONSTRAINT "{name}" {definition}'))

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Restore each original primary key and drop ``workspace_id``."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    is_mysql = op.get_bind().dialect.name == "mysql"
    for table, pk_cols in _TABLE_PKS.items():
        if is_mysql:
            pk_col_list = ", ".join(f"`{c}`" for c in pk_cols)
            op.execute(
                sa.text(
                    f"ALTER TABLE `{table}` "
                    f"DROP PRIMARY KEY, "
                    f"DROP COLUMN workspace_id, "
                    f"ADD CONSTRAINT `pk_{table}` PRIMARY KEY ({pk_col_list})"
                )
            )
            continue
        old_pk_name = None if sqlite else _existing_pk_name(table)
        with (
            _quiet_pk_override(),
            op.batch_alter_table(table, recreate="always" if sqlite else "auto") as batch_op,
        ):
            if old_pk_name is not None:
                batch_op.drop_constraint(old_pk_name, type_="primary")
            batch_op.drop_column("workspace_id")
            batch_op.create_primary_key(f"pk_{table}", pk_cols)

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
