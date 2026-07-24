"""Tests for the benchmark corpus seeder's bulk-insert fast path.

The seeder has two write strategies selected by dialect:

* the production store-ORM loop (one row/commit at a time) — the only path on
  non-SQLite dialects such as the nightly Postgres benchmark; and
* a SQLAlchemy-Core bulk-insert fast path (one transaction, ~10 batched
  ``executemany`` flushes) — SQLite only.

These tests pin the fast path to the store path's contract: the same corpus
*shape* (row counts, RNG-determined text, JSON ``data`` blobs, position
allocation, labels) so the read journeys measure the same volume either way.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

from omnigent.db.utils import get_or_create_engine
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

# ── helpers ──────────────────────────────────────────────────


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _s_of(title: str) -> int:
    """Recover the session index from a seeded title ``"bench session {s}: ..."``."""
    return int(title.split(":", 1)[0][len("bench session ") :])


def _titles_ordered(engine):
    """Titles keyed by session index s, sorted (RNG-determined, order-sensitive)."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT title FROM conversations")).all()
    return sorted((_s_of(t), t) for (t,) in rows)


def _item_text_ordered(engine):
    """(session s, position, search_text) sorted — the RNG draw sequence in order."""
    with engine.connect() as conn:
        convs = conn.execute(text("SELECT id, title FROM conversations")).all()
        s_by_cid = {cid: _s_of(t) for cid, t in convs}
        items = conn.execute(
            text("SELECT conversation_id, position, search_text FROM conversation_items")
        ).all()
    return sorted((s_by_cid[cid], pos, st) for cid, pos, st in items)


def _item_data_ordered(engine):
    """(session s, position, data-JSON) sorted — proves the serialization matches."""
    with engine.connect() as conn:
        convs = conn.execute(text("SELECT id, title FROM conversations")).all()
        s_by_cid = {cid: _s_of(t) for cid, t in convs}
        items = conn.execute(
            text("SELECT conversation_id, position, data FROM conversation_items")
        ).all()
    return sorted((s_by_cid[cid], pos, d) for cid, pos, d in items)


def _projects_ordered(engine):
    """(id, name, owner) for every project, sorted — project ids are deterministic."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, name, owner_user_id FROM projects")).all()
    return sorted((pid, name, owner) for pid, name, owner in rows)


def _membership_counts(engine):
    """(session s, project_id) for every filed session, sorted by session index.

    Session ids are fresh per run, so membership is keyed by the RNG-stable
    session index (recovered from the title) paired with the deterministic
    project id — proving both paths file the same sessions into the same folders.
    """
    with engine.connect() as conn:
        convs = conn.execute(text("SELECT id, title FROM conversations")).all()
        s_by_cid = {cid: _s_of(t) for cid, t in convs}
        rows = conn.execute(
            text(
                "SELECT id, project_id FROM omnigent_conversation_metadata "
                "WHERE project_id IS NOT NULL"
            )
        ).all()
    return sorted((s_by_cid[cid], pid) for cid, pid in rows)


# ── fast path: row counts + read path through the store API ──


def test_seed_fast_path_row_counts_and_read_path(tmp_path: Path) -> None:
    """The fast path writes every table the store path would, listable as "local"."""
    from dev.benchmarks.omnigent import seed as seed_mod

    db_uri = f"sqlite:///{tmp_path / 'fast.db'}"

    created = seed_mod.seed(
        db_uri, sessions=50, items_per_session=10, projects=5, filed_fraction=0.6, _fast=True
    )
    assert created == 50

    engine = get_or_create_engine(db_uri)
    assert _count(engine, "conversations") == 50
    assert _count(engine, "conversation_items") == 500
    assert _count(engine, "conversation_items_fts") == 500
    assert _count(engine, "agents") == 50
    assert _count(engine, "omnigent_conversation_metadata") == 50
    assert _count(engine, "session_permissions") == 50
    assert _count(engine, "users") == 1
    assert _count(engine, "conversation_labels") == 1
    assert _count(engine, "projects") == 5

    with engine.connect() as conn:
        # 30 of 50 sessions filed (0.6), spread round-robin across 5 projects,
        # each owned by "local"; the other 20 stay unfiled (project_id IS NULL).
        filed = conn.execute(
            text(
                "SELECT COUNT(*) FROM omnigent_conversation_metadata WHERE project_id IS NOT NULL"
            )
        ).scalar_one()
        assert filed == 30
        owners = conn.execute(text("SELECT DISTINCT owner_user_id FROM projects")).all()
        assert owners == [(RESERVED_USER_LOCAL,)]
        # Round-robin: each of the 5 projects holds 30/5 = 6 sessions.
        per_project = conn.execute(
            text(
                "SELECT project_id, COUNT(*) FROM omnigent_conversation_metadata "
                "WHERE project_id IS NOT NULL GROUP BY project_id"
            )
        ).all()
        assert sorted(c for _, c in per_project) == [6, 6, 6, 6, 6]

    with engine.connect() as conn:
        # next_position advanced to items_per_session on every conversation.
        nps = conn.execute(text("SELECT next_position FROM conversations")).all()
        assert all(r[0] == 10 for r in nps)
        # every grant is LEVEL_OWNER for the reserved "local" user.
        levels = conn.execute(
            text("SELECT DISTINCT level FROM session_permissions WHERE user_id = :u"),
            {"u": RESERVED_USER_LOCAL},
        ).all()
        assert levels == [(LEVEL_OWNER,)]
        # agent kind = session (2), metadata kind = default (1), archived = 0.
        assert {r[0] for r in conn.execute(text("SELECT kind FROM agents")).all()} == {2}
        assert {
            r[0]
            for r in conn.execute(text("SELECT kind FROM omnigent_conversation_metadata")).all()
        } == {1}
        assert {r[0] for r in conn.execute(text("SELECT archived FROM conversations")).all()} == {
            0
        }
        # the seed-meta label is present and carries the corpus config.
        labels = conn.execute(
            text("SELECT value FROM conversation_labels WHERE key = :k"),
            {"k": seed_mod._SEED_META_LABEL},
        ).all()
        assert len(labels) == 1
        assert labels[0][0].startswith("sessions=50;items=10;")

    # Read path the benchmark measures (store API, not raw SQL).
    conv = SqlAlchemyConversationStore(db_uri)
    listing = conv.list_conversations(
        limit=100,
        agent_name="bench-agent",
        accessible_by=RESERVED_USER_LOCAL,
        has_agent_id=True,
    )
    assert len(listing.data) == 50  # all seeded sessions listable as "local"
    items = conv.list_items(listing.data[0].id, limit=100)
    assert len(items.data) == 10
    # items come back in position order (response_id encodes the per-session index).
    assert [i.response_id for i in items.data] == [f"resp_seed_{i}" for i in range(10)]

    # Idempotent: a matching re-seed is a no-op (reuse-skip path).
    assert seed_mod.seed(db_uri, sessions=50, items_per_session=10, _fast=True) == 0


# ── byte-stability: fast path replicates the store path's corpus ──


def test_seed_fast_path_corpus_matches_store_path(tmp_path: Path) -> None:
    """Same config + RNG seed → identical corpus shape via either write path.

    Conversation/agent ids are fresh uuid4 per run, so we compare the
    RNG-determined content (titles, per-session search_text in draw order, JSON
    ``data`` blobs) and the row counts — proving the fast path draws the RNG in
    the same order and serializes rows identically to the store ORM loop. The
    project ids ARE deterministic (derived from the index), so project rows and
    per-project membership counts are compared directly.
    """
    from dev.benchmarks.omnigent import seed as seed_mod

    cfg = {
        "sessions": 50,
        "items_per_session": 10,
        "rng_seed": 1234,
        "projects": 5,
        "filed_fraction": 0.6,
    }
    fast_uri = f"sqlite:///{tmp_path / 'fast.db'}"
    slow_uri = f"sqlite:///{tmp_path / 'slow.db'}"

    assert seed_mod.seed(fast_uri, _fast=True, **cfg) == 50
    assert seed_mod.seed(slow_uri, _fast=False, **cfg) == 50  # force the store loop on SQLite

    fe = get_or_create_engine(fast_uri)
    se = get_or_create_engine(slow_uri)

    for table in (
        "conversations",
        "conversation_items",
        "conversation_items_fts",
        "agents",
        "omnigent_conversation_metadata",
        "session_permissions",
        "users",
        "conversation_labels",
        "projects",
    ):
        assert _count(fe, table) == _count(se, table), table

    # RNG draw order + content match exactly (order-preserving).
    assert _titles_ordered(fe) == _titles_ordered(se)
    assert _item_text_ordered(fe) == _item_text_ordered(se)
    # JSON serialization (default separators, exclude_none) matches byte-for-byte.
    assert _item_data_ordered(fe) == _item_data_ordered(se)
    # Deterministic project ids/names + membership counts match byte-for-byte.
    assert _projects_ordered(fe) == _projects_ordered(se)
    assert _membership_counts(fe) == _membership_counts(se)


# ── non-SQLite slow path (skipped unless a non-SQLite DB is provided) ──


def test_seed_slow_path_non_sqlite() -> None:
    """The store-API loop remains the path on non-SQLite dialects.

    Skipped unless ``OMNIGENT_BENCH_NONSQLITE_URI`` points at a real non-SQLite
    DB (e.g. the nightly Postgres benchmark). The fast path is SQLite-only, so
    this guards the fallback the nightly run relies on.
    """
    uri = os.environ.get("OMNIGENT_BENCH_NONSQLITE_URI")
    if not uri:
        pytest.skip(
            "set OMNIGENT_BENCH_NONSQLITE_URI to a non-SQLite URI to exercise the slow seed path"
        )
    assert make_url(uri).get_backend_name() != "sqlite"

    from dev.benchmarks.omnigent import seed as seed_mod

    # --reseed so the count assertion holds against a DB that may already have a corpus.
    created = seed_mod.seed(uri, sessions=20, items_per_session=5, reseed=True)
    assert created == 20

    conv = SqlAlchemyConversationStore(uri)
    listing = conv.list_conversations(limit=100, agent_name="bench-agent")
    assert len(listing.data) >= 20
