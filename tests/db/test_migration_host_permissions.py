"""Tests for the host-permissions migration's pre-release schema repair."""

from __future__ import annotations

import sqlalchemy as sa

from omnigent.db.migrations.versions import zz1a2b3c4d5e6_add_host_permissions as migration


class _Inspector:
    """Minimal reflected schema used by the PostgreSQL repair test."""

    def get_columns(self, _table: str) -> list[dict[str, object]]:
        return [
            {"name": "user_id", "type": sa.String(128)},
            {"name": "host_id", "type": sa.String(64)},
        ]

    def get_pk_constraint(self, _table: str) -> dict[str, str]:
        return {"name": "host_permissions_pkey"}

    def get_indexes(self, _table: str) -> list[dict[str, str]]:
        return [{"name": "ix_host_permissions_host_id"}]


class _Bind:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


def test_postgresql_repair_adds_workspace_and_converts_legacy_host_ids(
    monkeypatch,
) -> None:
    """An existing text-keyed table is upgraded instead of silently skipped."""
    bind = _Bind()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: calls.append(("drop_constraint", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda *args, **kwargs: calls.append(("drop_index", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda *args, **kwargs: calls.append(("add_column", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_primary_key",
        lambda *args, **kwargs: calls.append(("create_primary_key", *args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda *args, **kwargs: calls.append(("create_index", *args, kwargs)),
    )

    migration._upgrade_existing_postgresql(_Inspector())

    assert [call[0] for call in calls] == [
        "drop_constraint",
        "drop_index",
        "add_column",
        "create_primary_key",
        "create_index",
    ]
    assert len(bind.statements) == 1
    assert 'ALTER TABLE "host_permissions" ALTER COLUMN "host_id" TYPE bytea' in bind.statements[0]
    primary_key_call = calls[3]
    assert primary_key_call[3] == ["workspace_id", "user_id", "host_id"]
