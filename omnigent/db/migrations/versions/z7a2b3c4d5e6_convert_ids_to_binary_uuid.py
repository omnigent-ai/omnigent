"""Convert opaque uuid id columns from prefixed strings to 16-byte binary.

Revision ID: z7a2b3c4d5e6
Revises: z6a2b3c4d5e6
Create Date: 2026-07-09 00:00:00.000000

Our ids were opaque prefixed strings — ``ag_<hex>``, ``conv_<hex>``,
``host_<hex>``, per-type conversation-item prefixes (``msg_``/``fc_``/…),
``pol_<hex>``, and the dashed canonical uuid for comments. This migration drops
the prefixes and stores each id as the 16 raw bytes of its uuid: ``BYTEA``
(PostgreSQL), ``BLOB`` (SQLite / Cloudflare D1), ``BINARY(16)`` (MySQL) — the
``Uuid16`` column type. The rest of the system keeps the readable bare 32-char
hex form (entities, JSON blobs, URLs, the FTS mirror), so only the physical
column changes.

Columns deliberately NOT converted (kept as strings):
``omnigent_conversation_metadata.runner_id`` and
``conversation_items.response_id`` (polymorphic harness task tokens, not our
uuids), ``omnigent_conversation_metadata.external_session_id`` (harness-native),
``agents.bundle_location`` (a physical artifact-store key ``<agent_id>/<sha>``),
``account_tokens.id`` (a secret token), ``hosts.token_hash`` (a sha256), and the
email / username identity columns.

Strip rule (uniform): drop any dashes, take the trailing 32 hex chars, decode.
This reduces the ``conv_``/``ag_``/item-prefixed / dashed / already-bare forms
all to the same 16 bytes, and is idempotent on a bare id.

Total-transform fallback: a value whose trailing 32 chars are not valid hex
(hand-crafted junk such as an external monitor's ``host_fix_<epoch>`` /
``host_probe_<epoch>`` host id) would make ``decode``/``UNHEX``/``bytes.fromhex``
raise and abort the whole migration. Rather than block the deploy or drop the
row, such a value maps to ``md5(value)`` (16 bytes). Because ``md5`` is a pure
function of the string and identical across PostgreSQL, MySQL, and Python, a
junk value and every column that references it (e.g. ``hosts.host_id`` and the
``omnigent_conversation_metadata.host_id`` copies) map to the SAME bytes, so
cross-references still resolve. A well-formed id always takes the hex-decode
branch, so this changes nothing for normal data.

Also rewrites the embedded ``"session_id": "conv_<hex>"`` copy inside
``conversation_items.data`` (a plain ``Text`` column) and strips the mirrored
prefixes from the SQLite FTS shadow table, so those cross-references keep
resolving against the now-bare ids.

Downgrade restores string columns holding the bare 32-char hex form. It cannot
reintroduce the dropped prefixes — they carried no information (the item type
lives in ``conversation_items.type`` and ids are opaque) — so downgrade is
one-way on the prefix.

The MySQL path is modelled on standard MySQL semantics but is not exercised by
the local (SQLite) or CI test paths.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# A bare 32-char lowercase-hex uuid — the form every id reduces to after
# stripping dashes and any prefix. A value matching this is decoded directly;
# anything else is a non-uuid and falls back to md5 (see the module docstring).
_BARE_HEX_RE = re.compile(r"^[0-9a-f]{32}$")

revision: str = "z7a2b3c4d5e6"
down_revision: str | None = "z6a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Table -> id columns holding one of our opaque uuids. Every column here is
# ``String(64)`` before this migration and ``Uuid16`` (16 raw bytes) after.
_BINARY_ID_COLUMNS: dict[str, list[str]] = {
    "agents": ["id"],
    "files": ["id", "session_id"],
    "session_permissions": ["conversation_id"],
    "conversations": [
        "id",
        "parent_conversation_id",
        "root_conversation_id",
    ],
    # The conversations split (aa1b/bb2c) copied prefixed ids into these two
    # tables before this migration runs, so their copies are converted too.
    "omnigent_conversation_metadata": ["id", "host_id"],
    "agent_configuration": ["conversation_id", "agent_id"],
    "conversation_items": ["id", "conversation_id"],
    "conversation_labels": ["conversation_id"],
    "comments": ["id", "conversation_id"],
    "policies": ["id", "session_id"],
    "hosts": ["host_id"],
    # scheduled-task tables (added on main just before this migration). Their
    # own PKs (id, scheduled_task_id) are already created as Uuid16/binary by
    # that migration; only the string reference columns pointing at converted
    # tables need converting here.
    "scheduled_tasks": ["agent_id", "host_id", "last_run_conversation_id"],
    "scheduled_task_runs": ["conversation_id"],
}

_FTS_TABLE = "conversation_items_fts"
_FTS_DIALECTS = frozenset({"sqlite", "cloudflare_d1"})


def _id_to_bytes(value: object) -> bytes:
    """Strip prefix/dashes from an id string and return its 16 raw bytes.

    A value whose trailing 32 chars are valid hex is decoded; any other value
    (non-uuid junk) falls back to ``md5(value)`` so the conversion never fails.
    See the module docstring for why this preserves cross-references.
    """
    if isinstance(value, (bytes, bytearray)):  # already converted (idempotent)
        return bytes(value)
    text_value = str(value)
    bare = text_value.replace("-", "")[-32:]
    if _BARE_HEX_RE.match(bare):
        return bytes.fromhex(bare)
    # md5 is a stable remap for non-uuid junk, not a security primitive.
    return hashlib.md5(text_value.encode()).digest()


def _bytes_to_id(value: object) -> str:
    """Return the bare 32-char hex form of a stored 16-byte id (downgrade)."""
    if isinstance(value, str):  # already hex (idempotent)
        return value.replace("-", "")[-32:]
    return bytes(value).hex()


def _nullability(bind: sa.Connection) -> dict[tuple[str, str], bool]:
    """Reflect current NULL-ability for every converted column."""
    insp = sa.inspect(bind)
    result: dict[tuple[str, str], bool] = {}
    for table, cols in _BINARY_ID_COLUMNS.items():
        by_name = {c["name"]: bool(c["nullable"]) for c in insp.get_columns(table)}
        for col in cols:
            result[(table, col)] = by_name[col]
    return result


def _fts_present(bind: sa.Connection) -> bool:
    row = bind.execute(
        sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n").bindparams(
            n=_FTS_TABLE
        )
    ).first()
    return row is not None


# ── upgrade ─────────────────────────────────────────────


def upgrade() -> None:
    """Convert the id columns to 16-byte binary and fix the embedded copies."""
    bind = op.get_bind()
    dialect = bind.dialect.name
    nullable = _nullability(bind)

    if dialect == "postgresql":
        _upgrade_postgresql()
    elif dialect == "mysql":
        _upgrade_mysql(nullable)
    else:  # sqlite / cloudflare_d1
        _upgrade_sqlite(bind, nullable)

    _rewrite_embedded_session_id()

    if dialect in _FTS_DIALECTS and _fts_present(bind):
        op.execute(
            sa.text(
                f"UPDATE {_FTS_TABLE} SET "
                "item_id = substr(item_id, -32), "
                "conversation_id = substr(conversation_id, -32)"
            )
        )


def _upgrade_postgresql() -> None:
    """One atomic ALTER per column: strip prefix/dashes and decode hex -> bytea.

    A value whose trailing 32 chars are valid hex is decoded; any other value
    falls back to ``decode(md5(col), 'hex')`` — the same 16 bytes Python's
    ``_id_to_bytes`` and MySQL's ``UNHEX(MD5(col))`` produce — so the ALTER
    never raises on junk and referencing columns stay consistent.
    """
    for table, cols in _BINARY_ID_COLUMNS.items():
        for col in cols:
            stripped = f"right(replace(\"{col}\", '-', ''), 32)"
            op.execute(
                sa.text(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE bytea USING '
                    f"CASE WHEN {stripped} ~ '^[0-9a-f]{{32}}$' "
                    f"THEN decode({stripped}, 'hex') "
                    f"ELSE decode(md5(\"{col}\"), 'hex') END"
                )
            )


def _upgrade_mysql(nullable: dict[tuple[str, str], bool]) -> None:
    """Reinterpret as binary, decode the trailing 32 chars, then fix to BINARY(16).

    A value whose trailing 32 chars are valid hex is decoded via ``UNHEX``; any
    other value falls back to ``UNHEX(MD5(col))`` — the same 16 bytes the SQLite
    (``_id_to_bytes``) and PostgreSQL (``decode(md5(col),'hex')``) paths produce
    — so ``UNHEX`` never returns NULL on junk and referencing columns stay
    consistent. A post-UPDATE NULL guard remains as a belt-and-braces check that
    no non-NULL value slipped through to NULL before the NOT NULL type change.

    The interim ``VARBINARY(64)`` reinterpret keeps the column's real
    nullability (``null_sql``): MySQL rejects making a PRIMARY KEY column NULL
    even transiently (error 1171), and the CASE/UNHEX always yields 16 bytes,
    so no NOT NULL column ever needs to hold NULL mid-conversion.

    The value expression reads the column through ``CONVERT(... USING utf8mb4)``:
    after the interim reinterpret the column is binary, and MySQL refuses
    ``REGEXP`` on a binary string against a utf8mb4 pattern (error 3995), so the
    original ASCII hex is recovered as text before the regex/UNHEX/MD5 run.
    """
    bind = op.get_bind()
    for table, cols in _BINARY_ID_COLUMNS.items():
        for col in cols:
            null_sql = "NULL" if nullable[(table, col)] else "NOT NULL"
            count_nulls = sa.text(f"SELECT COUNT(*) FROM `{table}` WHERE `{col}` IS NULL")
            nulls_before = bind.execute(count_nulls).scalar_one()
            op.execute(sa.text(f"ALTER TABLE `{table}` MODIFY `{col}` VARBINARY(64) {null_sql}"))
            col_text = f"CONVERT(`{col}` USING utf8mb4)"  # binary -> text for regex/UNHEX/MD5
            stripped = f"RIGHT(REPLACE({col_text}, '-', ''), 32)"
            op.execute(
                sa.text(
                    f"UPDATE `{table}` SET `{col}` = "
                    f"CASE WHEN {stripped} REGEXP '^[0-9a-f]{{32}}$' "
                    f"THEN UNHEX({stripped}) "
                    f"ELSE UNHEX(MD5({col_text})) END "
                    f"WHERE `{col}` IS NOT NULL"
                )
            )
            nulls_after = bind.execute(count_nulls).scalar_one()
            if nulls_after != nulls_before:
                raise RuntimeError(
                    f"id conversion would lose data: {nulls_after - nulls_before} "
                    f"value(s) in `{table}`.`{col}` unexpectedly became NULL; "
                    f"aborting before the type change"
                )
            op.execute(sa.text(f"ALTER TABLE `{table}` MODIFY `{col}` BINARY(16) {null_sql}"))


def _upgrade_sqlite(bind: sa.Connection, nullable: dict[tuple[str, str], bool]) -> None:
    """Convert values to raw bytes in place, then change the declared type to BLOB.

    A bound ``bytes`` value is stored verbatim as a BLOB even while the column is
    still declared ``TEXT`` — SQLite's TEXT affinity does not coerce a BLOB — so
    the subsequent batch type change copies real 16-byte values, not hex text.
    """
    op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    for table, cols in _BINARY_ID_COLUMNS.items():
        select_cols = ", ".join(f'"{c}"' for c in cols)
        rows = bind.execute(sa.text(f'SELECT rowid, {select_cols} FROM "{table}"')).fetchall()
        for row in rows:
            assignments = {
                col: _id_to_bytes(row[idx])
                for idx, col in enumerate(cols, start=1)
                if row[idx] is not None
            }
            if assignments:
                set_clause = ", ".join(f'"{c}" = :{c}' for c in assignments)
                bind.execute(
                    sa.text(f'UPDATE "{table}" SET {set_clause} WHERE rowid = :__rowid'),
                    {**assignments, "__rowid": row[0]},
                )

    for table, cols in _BINARY_ID_COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            for col in cols:
                batch.alter_column(
                    col,
                    type_=sa.LargeBinary(16),
                    existing_type=sa.String(64),
                    existing_nullable=nullable[(table, col)],
                )

    op.execute(sa.text("PRAGMA foreign_keys = ON"))


def _rewrite_embedded_session_id() -> None:
    """Strip the ``conv_`` prefix from the ``session_id`` echoed inside
    ``conversation_items.data`` (plain Text; both JSON spacings handled).

    Scoped to ``type = 8`` (the ``resource_event`` enum code): only that item
    type carries a structural ``session_id`` field. Message items may contain
    the same byte sequence inside user/assistant prose (pasted JSON, debug
    transcripts) — rewriting those would silently corrupt chat history.
    """
    for old in ('"session_id": "conv_', '"session_id":"conv_'):
        new = old.replace("conv_", "")
        op.execute(
            sa.text(
                "UPDATE conversation_items SET data = REPLACE(data, :old, :new) "
                "WHERE type = 8 AND data LIKE :like"
            ).bindparams(old=old, new=new, like=f"%{old}%")
        )


# ── downgrade ───────────────────────────────────────────


def downgrade() -> None:
    """Restore String(64) columns holding the bare 32-char hex form (no prefix)."""
    bind = op.get_bind()
    dialect = bind.dialect.name
    nullable = _nullability(bind)

    if dialect == "postgresql":
        for table, cols in _BINARY_ID_COLUMNS.items():
            for col in cols:
                op.execute(
                    sa.text(
                        f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE varchar(64) '
                        f"USING encode(\"{col}\", 'hex')"
                    )
                )
    elif dialect == "mysql":
        for table, cols in _BINARY_ID_COLUMNS.items():
            for col in cols:
                null_sql = "NULL" if nullable[(table, col)] else "NOT NULL"
                # Interim reinterpret keeps real nullability — MySQL rejects a
                # transiently-NULL PK column (error 1171).
                op.execute(
                    sa.text(f"ALTER TABLE `{table}` MODIFY `{col}` VARBINARY(64) {null_sql}")
                )
                op.execute(
                    sa.text(
                        f"UPDATE `{table}` SET `{col}` = LOWER(HEX(`{col}`)) "
                        f"WHERE `{col}` IS NOT NULL"
                    )
                )
                op.execute(sa.text(f"ALTER TABLE `{table}` MODIFY `{col}` VARCHAR(64) {null_sql}"))
    else:  # sqlite / cloudflare_d1
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))
        for table, cols in _BINARY_ID_COLUMNS.items():
            select_cols = ", ".join(f'"{c}"' for c in cols)
            rows = bind.execute(sa.text(f'SELECT rowid, {select_cols} FROM "{table}"')).fetchall()
            for row in rows:
                assignments = {
                    col: _bytes_to_id(row[idx])
                    for idx, col in enumerate(cols, start=1)
                    if row[idx] is not None
                }
                if assignments:
                    set_clause = ", ".join(f'"{c}" = :{c}' for c in assignments)
                    bind.execute(
                        sa.text(f'UPDATE "{table}" SET {set_clause} WHERE rowid = :__rowid'),
                        {**assignments, "__rowid": row[0]},
                    )
        for table, cols in _BINARY_ID_COLUMNS.items():
            with op.batch_alter_table(table) as batch:
                for col in cols:
                    batch.alter_column(
                        col,
                        type_=sa.String(64),
                        existing_type=sa.LargeBinary(16),
                        existing_nullable=nullable[(table, col)],
                    )
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
