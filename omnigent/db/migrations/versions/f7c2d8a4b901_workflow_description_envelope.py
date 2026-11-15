"""Store workflow descriptions in bounded binary storage envelopes.

Stop writers and apply before upgrading the application. Existing descriptions
are preserved; new writes use the same plaintext/ciphertext
envelope as journal content and saved plans. No content encryption is enabled.
For rollback, stop writers and downgrade before starting the older application.
Downgrade restores descriptions in bounded batches without deleting workflows.
Encrypted descriptions must be decrypted by their deployment before downgrade.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext

import sqlalchemy as sa
import zstandard
from alembic import op
from sqlalchemy.dialects.mysql import VARBINARY

revision: str = "f7c2d8a4b901"
down_revision: str | None = "e9b6a17c20d4"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_BATCH_SIZE = 250
_BINARY = sa.LargeBinary().with_variant(VARBINARY(16_383), "mysql")


def _alter_type(*, to_binary: bool) -> None:
    sqlite = op.get_bind().dialect.name in ("sqlite", "cloudflare_d1")
    with op.batch_alter_table("workflows", recreate="always" if sqlite else "auto") as batch:
        batch.alter_column(
            "description",
            existing_type=sa.String(2000) if to_binary else _BINARY,
            type_=_BINARY if to_binary else sa.String(2000),
            existing_nullable=False,
            postgresql_using=(
                "convert_to(description, 'UTF8')"
                if to_binary
                else "convert_from(description, 'UTF8')"
            ),
        )


def upgrade() -> None:
    """Preserve existing descriptions as UTF-8 bytes."""
    if op.get_bind().dialect.name == "cockroachdb":
        _copy_cockroach_description(to_binary=True)
        return
    if op.get_bind().dialect.name in ("sqlite", "cloudflare_d1"):
        # SQLite text can start with NUL. Frame it before casting to binary so
        # the reader cannot mistake legacy user text for a storage envelope.
        _rewrite_descriptions(
            lambda value: (
                b"\x00\x00" + value.encode("utf-8")
                if isinstance(value, str) and value.startswith("\x00")
                else None
            )
        )
    _alter_type(to_binary=True)


def _plaintext(value: str | bytes | memoryview) -> str:
    # Freeze the on-disk format here so application codec changes cannot alter rollback.
    if isinstance(value, str):
        return value
    data = bytes(value)
    if data.startswith(b"\x00\x00"):
        return data[2:].decode("utf-8")
    if data.startswith(b"\x00\x01"):
        return zstandard.ZstdDecompressor().decompress(data[2:]).decode("utf-8")
    if data.startswith(b"\x00"):
        raise ValueError("Decrypt workflow descriptions before downgrading their storage")
    return data.decode("utf-8")


def _rewrite_descriptions(
    transform: Callable[[str | bytes | memoryview], str | bytes | None],
    *,
    target: str = "description",
) -> None:
    connection = op.get_bind()
    workflows = sa.table(
        "workflows",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id"),
        sa.column("description"),
    )
    if target != "description":
        workflows.append_column(sa.column(target))
    query = sa.select(workflows.c.workspace_id, workflows.c.id, workflows.c.description).order_by(
        workflows.c.workspace_id, workflows.c.id
    )
    page = connection.execute(query.limit(_BATCH_SIZE)).all()
    cockroach = connection.dialect.name == "cockroachdb"
    if page:
        with nullcontext() if cockroach else op.get_context().autocommit_block():
            while page:
                for workspace_id, row_id, description in page:
                    value = transform(description)
                    if value is None:
                        continue
                    connection.execute(
                        workflows.update()
                        .where(
                            workflows.c.workspace_id == workspace_id,
                            workflows.c.id == row_id,
                        )
                        .values({target: value})
                    )
                if cockroach:
                    _publish_cockroach_schema(connection)
                workspace_id, row_id = page[-1].workspace_id, page[-1].id
                page = connection.execute(
                    query.where(
                        sa.or_(
                            workflows.c.workspace_id > workspace_id,
                            sa.and_(
                                workflows.c.workspace_id == workspace_id,
                                workflows.c.id > row_id,
                            ),
                        )
                    ).limit(_BATCH_SIZE)
                ).all()


def _publish_cockroach_schema(connection: sa.Connection) -> None:
    # CockroachDB must publish schema changes before the copy or next DDL.
    connection.commit()
    connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _copy_cockroach_description(*, to_binary: bool) -> None:
    """Avoid CockroachDB's experimental text/bytes ALTER COLUMN TYPE."""
    connection = op.get_bind()
    temporary = "_description_bytes" if to_binary else "_description_text"
    target_type = _BINARY if to_binary else sa.String(2000)
    columns = {
        column["name"]: column["type"]
        for column in sa.inspect(connection).get_columns("workflows")
    }
    if temporary not in columns:
        # DDL may commit before Alembic records the revision; a retry is harmless.
        if isinstance(columns["description"], sa.LargeBinary) == to_binary:
            return
        op.add_column("workflows", sa.Column(temporary, target_type, nullable=True))
        _publish_cockroach_schema(connection)

    if "description" in columns:
        _rewrite_descriptions(
            lambda value: (
                b"\x00\x00" + _plaintext(value).encode("utf-8") if to_binary else _plaintext(value)
            ),
            target=temporary,
        )
        _publish_cockroach_schema(connection)
        with op.batch_alter_table("workflows") as batch:
            batch.drop_column("description")
        _publish_cockroach_schema(connection)

    # Resume even if the source was already dropped by a nontransactional DDL.
    with op.batch_alter_table("workflows") as batch:
        batch.alter_column(temporary, existing_type=target_type, nullable=False)
    _publish_cockroach_schema(connection)
    with op.batch_alter_table("workflows") as batch:
        batch.alter_column(temporary, new_column_name="description", existing_type=target_type)
    _publish_cockroach_schema(connection)


def downgrade() -> None:
    """Restore UTF-8 descriptions before converting the column back to text."""
    if op.get_bind().dialect.name == "cockroachdb":
        _copy_cockroach_description(to_binary=False)
        return
    sqlite = op.get_bind().dialect.name in ("sqlite", "cloudflare_d1")
    _rewrite_descriptions(
        lambda value: _plaintext(value) if sqlite else _plaintext(value).encode("utf-8")
    )
    _alter_type(to_binary=False)
