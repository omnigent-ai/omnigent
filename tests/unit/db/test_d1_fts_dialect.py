"""Integration test: full-text search works on the Cloudflare D1 dialect.

D1 is SQLite served over an HTTP REST API. The third-party `cloudflare_d1`
SQLAlchemy dialect POSTs every statement to ``{base}/raw``. Here we mock that
endpoint with `respx` (the standard HTTPX mock library) and back it with an
in-memory `sqlite3` connection — so the *real* dialect runs against the *real*
engine D1 uses, with only the network hop faked.

This guards the FTS-gate change in ``db/utils.py``: ``ensure_fts_table`` /
``insert_fts`` / ``delete_fts_by_conversation`` must fire on the ``cloudflare_d1``
dialect (they no-op'd before, gating on the literal name ``"sqlite"``), so search
works on a D1-backed deployment.
"""

import json
import sqlite3

import httpx
import pytest
import respx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from omnigent.db.utils import (
    _FTS_TABLE,
    _supports_fts5,
    delete_fts_by_conversation,
    ensure_fts_table,
    insert_fts,
)

_D1_BASE = "http://d1.test/db"
_TXN_KEYWORDS = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}


def _d1_response(columns, rows):
    """Build the D1 ``/raw`` success envelope the dialect parses."""
    return {
        "success": True,
        "result": [{"results": {"columns": columns, "rows": rows}, "meta": {}, "success": True}],
    }


@pytest.fixture()
def d1_engine(monkeypatch):
    """
    A real ``cloudflare_d1`` SQLAlchemy engine whose ``/raw`` HTTP endpoint is
    served from one in-memory SQLite connection (autocommit, like D1).
    """
    # The cloudflare_d1 dialect is a deploy/test dependency (pyproject [dev]);
    # skip gracefully if a minimal install lacks it.
    pytest.importorskip("sqlalchemy_cloudflare_d1")
    monkeypatch.setenv("CF_D1_BASE_URL", _D1_BASE)
    backing = sqlite3.connect(":memory:", isolation_level=None)  # autocommit, like D1

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sql = body["sql"]
        params = body.get("params") or []
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in _TXN_KEYWORDS:  # D1 auto-commits; ignore transaction control
            return httpx.Response(200, json=_d1_response([], []))
        cur = backing.execute(sql, params)
        if cur.description:  # a SELECT / PRAGMA
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        else:
            columns, rows = [], []
        return httpx.Response(200, json=_d1_response(columns, rows))

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


def test_fts_lifecycle_on_d1(d1_engine):
    """End-to-end FTS on a D1 engine: create the index, write to it, search it,
    clear it — all through the real dialect over the mocked REST API."""
    # 1. ensure_fts_table now fires on cloudflare_d1 (it no-op'd before the fix).
    ensure_fts_table(d1_engine)
    with d1_engine.connect() as conn:  # the virtual table must now exist
        conn.execute(text(f"SELECT item_id FROM {_FTS_TABLE}")).fetchall()

    # 2. Index two items; only one matches the query.
    with Session(d1_engine) as s:
        insert_fts(s, "msg_1", "conv_a", "the quick brown fox")
        insert_fts(s, "msg_2", "conv_a", "lazy dog sleeps")
        s.commit()

    # 3. Full-text MATCH finds the right row.
    with d1_engine.connect() as conn:
        hits = conn.execute(
            text(f"SELECT item_id FROM {_FTS_TABLE} WHERE search_text MATCH :q"),
            {"q": "fox"},
        ).fetchall()
    assert [r[0] for r in hits] == ["msg_1"]

    # 4. delete_fts_by_conversation clears the conversation's rows.
    with Session(d1_engine) as s:
        delete_fts_by_conversation(s, "conv_a")
        s.commit()
    with d1_engine.connect() as conn:
        remaining = conn.execute(text(f"SELECT count(*) FROM {_FTS_TABLE}")).scalar()
    assert remaining == 0
