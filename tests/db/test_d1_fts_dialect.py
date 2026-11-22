"""Migration and full-text search tests for the Cloudflare D1 dialect.

D1 is SQLite served over an HTTP REST API. The third-party `cloudflare_d1`
SQLAlchemy dialect POSTs every statement to ``{base}/raw``. Here we mock that
endpoint with `respx` (the standard HTTPX mock library) and back it with an
local `sqlite3` connection — so the *real* dialect runs against the *real*
engine D1 uses, with only the network hop faked.

This guards the FTS-gate change in ``db/utils.py``: ``ensure_fts_table`` /
``insert_fts`` / ``delete_fts_by_conversation`` must fire on the ``cloudflare_d1``
dialect (they no-op'd before, gating on the literal name ``"sqlite"``), so search
works on a D1-backed deployment.
The migration regression loads the deployment shim and upgrades a populated
pre-workspace schema to head without transaction SQL or temporary-schema access.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
import respx
from alembic import command
from alembic.ddl import impl
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import registry
from sqlalchemy.orm import Session

from omnigent.db.utils import (
    _FTS_TABLE,
    _build_alembic_config,
    _get_current_db_revision,
    _get_head_db_revision,
    _supports_fts5,
    delete_fts_by_conversation,
    ensure_fts_table,
    insert_fts,
)

_D1_BASE = "http://d1.test/db"
_TXN_KEYWORDS = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}


@pytest.fixture()
def d1_deployment_shim(monkeypatch):
    monkeypatch.setattr(registry, "impls", registry.impls.copy())
    monkeypatch.setattr(impl, "_impls", impl._impls.copy())
    path = Path(__file__).resolve().parents[2] / "deploy/cloudflare/sitecustomize.py"
    spec = importlib.util.spec_from_file_location("_d1_deployment_shim", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)


def _d1_response(columns, rows, *, changes=0, last_row_id=None):
    """Build the D1 ``/raw`` success envelope the dialect parses."""
    return {
        "success": True,
        "result": [
            {
                "results": {"columns": columns, "rows": rows},
                "meta": {"changes": changes, "last_row_id": last_row_id},
                "success": True,
            }
        ],
    }


@pytest.fixture()
def d1_engine(monkeypatch, tmp_path, request):
    """
    A real ``cloudflare_d1`` SQLAlchemy engine whose ``/raw`` HTTP endpoint is
    served from one SQLite connection (autocommit, like D1).
    """
    # The cloudflare_d1 dialect is in the repository's test dependency group;
    # skip gracefully if a minimal install lacks it.
    pytest.importorskip("sqlalchemy_cloudflare_d1")
    monkeypatch.setenv("CF_D1_BASE_URL", _D1_BASE)
    path = tmp_path / "d1.db"
    seed_revision = getattr(request, "param", None)
    if seed_revision is not None:
        request.getfixturevalue("d1_deployment_shim")
        seed_uri = f"sqlite:///{path}"
        seed_engine = create_engine(seed_uri)
        try:
            config = _build_alembic_config(seed_uri)
            with seed_engine.begin() as conn:
                config.attributes["connection"] = conn
                command.upgrade(config, seed_revision)
                conn.execute(text("INSERT INTO users (id, is_admin) VALUES ('migration-user', 0)"))
        finally:
            seed_engine.dispose()
    backing = sqlite3.connect(path, isolation_level=None)  # autocommit, like D1

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sql = body["sql"]
        params = body.get("params") or []
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in _TXN_KEYWORDS:
            return httpx.Response(
                400, json={"success": False, "errors": [{"message": "D1 rejects transactions"}]}
            )
        if seed_revision is not None and (
            "temp." in sql.lower()
            or "sqlite_temp_master" in sql.lower()
            or "database_list" in sql.lower()
        ):
            return httpx.Response(
                400, json={"success": False, "errors": [{"message": "D1 forbids temp schema"}]}
            )
        cur = backing.execute(sql, params)
        if cur.description:  # a SELECT / PRAGMA
            columns = [d[0] for d in cur.description]
            rows = [
                [list(value) if isinstance(value, bytes) else value for value in row]
                for row in cur.fetchall()
            ]
        else:
            columns, rows = [], []
        return httpx.Response(
            200,
            json=_d1_response(
                columns, rows, changes=max(cur.rowcount, 0), last_row_id=cur.lastrowid
            ),
        )

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{_D1_BASE}/raw").mock(side_effect=handler)
        engine = create_engine("cloudflare_d1://acct:token@db")
        try:
            yield engine
        finally:
            engine.dispose()
            backing.close()


def test_fts5_gate_includes_d1():
    """The gate that decides whether FTS runs must accept D1, reject Postgres."""
    assert _supports_fts5("cloudflare_d1") is True
    assert _supports_fts5("sqlite") is True
    assert _supports_fts5("postgresql") is False


@pytest.mark.parametrize("d1_engine", ["q1a2b3c4d5e6"], indirect=True)
def test_upgrade_from_pre_workspace_revision_on_d1(d1_engine):
    """Upgrade the reported stale revision through the real D1 HTTP dialect."""
    uri = str(d1_engine.url)
    config = _build_alembic_config(uri)
    with d1_engine.connect() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
        conn.commit()

    assert _get_current_db_revision(d1_engine) == _get_head_db_revision(uri)
    with d1_engine.connect() as conn:
        row = conn.execute(
            text("SELECT workspace_id, id FROM users WHERE id = 'migration-user'")
        ).one()
    assert row == (0, "migration-user")


def test_fts_lifecycle_on_d1(d1_engine):
    """End-to-end FTS on a D1 engine: create the index, write to it, search it,
    clear it — all through the real dialect over the mocked REST API."""
    # 1. ensure_fts_table now fires on cloudflare_d1 (it no-op'd before the fix).
    ensure_fts_table(d1_engine)
    with d1_engine.connect() as conn:  # the virtual table must now exist
        conn.execute(text(f"SELECT item_id FROM {_FTS_TABLE}")).fetchall()

    # 2. Index two items; only one matches the query.
    with Session(d1_engine) as s:
        insert_fts(
            s,
            "9980c8a9248139f14f4165e5d53088aa",
            "94c349190e241f85a984b3df8f129696",
            "the quick brown fox",
        )
        insert_fts(
            s,
            "0fd4e86b2daa009cd9929641dbd7dab6",
            "94c349190e241f85a984b3df8f129696",
            "lazy dog sleeps",
        )
        s.commit()

    # 3. Full-text MATCH finds the right row.
    with d1_engine.connect() as conn:
        hits = conn.execute(
            text(f"SELECT item_id FROM {_FTS_TABLE} WHERE search_text MATCH :q"),
            {"q": "fox"},
        ).fetchall()
    assert [r[0] for r in hits] == ["9980c8a9248139f14f4165e5d53088aa"]

    # 4. delete_fts_by_conversation clears the conversation's rows.
    with Session(d1_engine) as s:
        delete_fts_by_conversation(s, "94c349190e241f85a984b3df8f129696")
        s.commit()
    with d1_engine.connect() as conn:
        remaining = conn.execute(text(f"SELECT count(*) FROM {_FTS_TABLE}")).scalar()
    assert remaining == 0
