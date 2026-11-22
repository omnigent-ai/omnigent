"""Cloudflare D1 deployments migrate to head through the container entrypoint.

The Cloudflare image auto-loads ``deploy/cloudflare/sitecustomize.py``, which
registers ``cloudflare_d1`` as a SQLite dialect over D1's HTTP API. Here the
real shim and dialect run against a mocked ``/raw`` endpoint backed by SQLite.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import alembic.ddl.impl
import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects import registry

from deploy.docker.entrypoint import run_migrations
from omnigent.db.utils import _build_alembic_config, _get_head_db_revision

respx = pytest.importorskip("respx")
upstream_d1 = pytest.importorskip("sqlalchemy_cloudflare_d1.dialect")

_D1_SHIM = Path(__file__).resolve().parents[2] / "deploy" / "cloudflare" / "sitecustomize.py"
_D1_BASE = "http://d1.test/db"
_D1_URL = "cloudflare_d1://acct-example:token-example@db"
_TRANSACTION_CONTROL = re.compile(r"\s*(BEGIN|SAVEPOINT|RELEASE|ROLLBACK\s+TO)\b", re.IGNORECASE)
_TEMP_SCHEMA = re.compile(
    r"\btemp\s*\.|\"temp\"\s*\.|sqlite_temp_master|\bdatabase_list\b|\bCREATE\s+TEMP",
    re.IGNORECASE,
)
_NOW = 1_780_000_000
_Q1_ROWS = (
    "INSERT INTO users (id, is_admin, password_hash, created_at) VALUES ('owner', 1, NULL, :t)",
    "INSERT INTO agents (id, created_at, name, bundle_location, version, kind) "
    "VALUES ('agent-1', :t, 'hello_world', 'agents/hello_world', 1, 'template')",
    "INSERT INTO conversations (id, created_at, updated_at, title, kind, agent_id, "
    "root_conversation_id, archived) "
    "VALUES ('conv-1', :t, :t, 'existing session', 'default', 'agent-1', 'conv-1', 0)",
    "INSERT INTO conversation_items (id, conversation_id, response_id, created_at, "
    "status, position, type, data, search_text) VALUES ('msg-1', 'conv-1', 'resp-1', "
    ":t, 'completed', 0, 'message', '{\"role\": \"user\", \"content\": \"hi\"}', 'hi')",
    "INSERT INTO hosts (owner, name, host_id, status, created_at, updated_at) "
    "VALUES ('owner', 'laptop', 'host-1', 'offline', :t, :t)",
)


def _d1_error(message: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "result": [],
            "success": False,
            "errors": [{"code": 7500, "message": message}],
            "messages": [],
        },
    )


class _D1Api:
    """D1's ``/raw`` endpoint over one autocommit SQLite file, refusing what D1 refuses."""

    def __init__(self, path: Path) -> None:
        self.backing = sqlite3.connect(path, isolation_level=None)
        self.rejected_transaction_control: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sql, params = body["sql"], body.get("params") or []
        if _TRANSACTION_CONTROL.match(sql):
            self.rejected_transaction_control.append(sql)
            return _d1_error(
                "transaction statements are not supported over the D1 API: SQLITE_ERROR"
            )
        if _TEMP_SCHEMA.search(sql):
            return _d1_error("not authorized: SQLITE_AUTH")
        try:
            cursor = self.backing.execute(sql, params)
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            return _d1_error(f"{exc}: SQLITE_ERROR")
        changes = max(cursor.rowcount, 0)
        encoded = [
            [base64.b64encode(v).decode() if isinstance(v, bytes) else v for v in row]
            for row in rows
        ]
        result = {
            "success": True,
            "results": {"columns": [c[0] for c in cursor.description or []], "rows": encoded},
            "meta": {"changes": changes, "last_row_id": cursor.lastrowid or 0},
        }
        return httpx.Response(
            200, json={"success": True, "errors": [], "messages": [], "result": [result]}
        )


@pytest.fixture
def d1_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_D1Api]:
    monkeypatch.setattr(registry, "impls", dict(registry.impls))
    monkeypatch.setattr(alembic.ddl.impl, "_impls", dict(alembic.ddl.impl._impls))
    dbapi = upstream_d1.CloudflareD1Dialect.import_dbapi()
    monkeypatch.setattr(dbapi, "sqlite_version_info", (3, 45, 0), raising=False)
    monkeypatch.setattr(dbapi, "sqlite_version", "3.45.0", raising=False)
    spec = importlib.util.spec_from_file_location("cloudflare_d1_sitecustomize", _D1_SHIM)
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, shim)
    spec.loader.exec_module(shim)
    monkeypatch.setenv("CF_D1_BASE_URL", _D1_BASE)

    api = _D1Api(tmp_path / "d1.sqlite")
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{_D1_BASE}/raw").mock(side_effect=api)
        engine = sa.create_engine(_D1_URL)
        assert isinstance(engine.dialect, shim.CloudflareD1Dialect)
        engine.dispose()
        yield api
    api.backing.close()


def _revision(connection: sqlite3.Connection) -> str:
    return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def _schema(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    tables = [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    schema: dict[str, dict[str, object]] = {}
    for table in tables:
        indexes = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
        schema[table] = {
            "columns": sorted(
                row[1:] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ),
            "foreign_keys": sorted(
                row[2:] for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
            ),
            "indexes": {
                name: (
                    unique,
                    partial,
                    [
                        (row[0], row[2], row[3], row[5])
                        for row in connection.execute(f'PRAGMA index_xinfo("{name}")')
                    ],
                )
                for _, name, unique, _, partial in indexes
            },
        }
    return schema


def test_fresh_d1_database_migrates_to_head_with_the_sqlite_schema(
    d1_api: _D1Api, tmp_path: Path
) -> None:
    run_migrations(_D1_URL)
    native_path = tmp_path / "native.sqlite"
    run_migrations(f"sqlite:///{native_path}")

    assert _revision(d1_api.backing) == _get_head_db_revision(_D1_URL)
    native = sqlite3.connect(native_path)
    try:
        assert _schema(d1_api.backing) == _schema(native)
    finally:
        native.close()
    assert d1_api.rejected_transaction_control == []


_Q1_BINARY_SOURCES = (
    "UPDATE agents SET description = 'Test'",
    "UPDATE conversations SET session_state = '{\"k\": 1}'",
    "UPDATE hosts SET configured_harnesses = '[\"codex\"]'",
    "INSERT INTO policies (id, name, created_at, updated_at, type, enabled, session_id, "
    "handler, created_by, scope) "
    "VALUES ('pol-1', 'guard', :t, :t, 'python', 1, 'conv-1', 'pkg:check', 'owner', 'session')",
)


def _upgrade_from_seeded_q1(url: str) -> sa.Engine:
    engine = sa.create_engine(url)
    config = _build_alembic_config(url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "q1a2b3c4d5e6")
        for statement in (*_Q1_ROWS, *_Q1_BINARY_SOURCES):
            connection.execute(sa.text(statement), {"t": _NOW})
        connection.commit()
    engine.dispose()
    run_migrations(url)
    return sa.create_engine(url)


def _rows(engine: sa.Engine, table_name: str) -> list[tuple[object, ...]]:
    with engine.connect() as connection:
        table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
        return [tuple(row) for row in connection.execute(sa.select(table).order_by(*table.c))]


def test_d1_database_left_at_q1_upgrades_to_head_and_keeps_its_rows(
    d1_api: _D1Api, tmp_path: Path
) -> None:
    native = _upgrade_from_seeded_q1(f"sqlite:///{tmp_path / 'native.sqlite'}")
    d1 = _upgrade_from_seeded_q1(_D1_URL)

    assert _revision(d1_api.backing) == _get_head_db_revision(_D1_URL)
    assert d1_api.rejected_transaction_control == []
    for table_name in ("agents", "conversations", "conversation_items", "hosts", "policies"):
        rows = _rows(d1, table_name)
        assert len(rows) == 1, table_name
        assert rows == _rows(native, table_name), table_name
    with native.connect() as connection:
        conversations = sa.Table("conversations", sa.MetaData(), autoload_with=connection)
        conversation_id = connection.execute(sa.select(conversations.c.id)).scalar_one()
    with d1.connect() as connection:
        conversations = sa.Table("conversations", sa.MetaData(), autoload_with=connection)
        title = connection.execute(
            sa.select(conversations.c.title).where(conversations.c.id == conversation_id)
        ).scalar_one()
    assert title == "existing session"
    d1.dispose()
    native.dispose()
