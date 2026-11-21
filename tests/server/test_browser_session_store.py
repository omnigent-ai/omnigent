"""Tests for :class:`BrowserSessionRevocationStore` (durable logout record)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select, text

from omnigent.db.db_models import SqlBrowserSessionRevocation, workspace_scope
from omnigent.server import browser_session_store
from omnigent.server.browser_session_store import BrowserSessionRevocationStore


@pytest.fixture
def store(db_uri: str) -> BrowserSessionRevocationStore:
    return BrowserSessionRevocationStore(db_uri)


def _sids(store: BrowserSessionRevocationStore) -> set[str]:
    with store._session("test_list_browser_session_revocations") as session:
        return set(session.scalars(select(SqlBrowserSessionRevocation.sid)))


def _rows(store: BrowserSessionRevocationStore) -> set[tuple[int, str]]:
    """Every row's (workspace_id, sid), across all workspaces."""
    with store._session("test_list_all_browser_session_revocations") as session:
        rows = session.execute(
            select(SqlBrowserSessionRevocation.workspace_id, SqlBrowserSessionRevocation.sid)
        )
        return {(workspace_id, sid) for workspace_id, sid in rows}


def test_revoked_sid_is_reported_until_it_lapses(store: BrowserSessionRevocationStore) -> None:
    now = int(time.time())
    store.revoke("sid-a", user_id="alice", expires_at=now + 3600)

    assert store.is_revoked("sid-a")
    assert not store.is_revoked("sid-unknown")


def test_revocation_is_shared_across_store_instances(db_uri: str) -> None:
    """A second store on the same database (another replica, or a restart) sees it."""
    BrowserSessionRevocationStore(db_uri).revoke(
        "sid-a", user_id="alice", expires_at=int(time.time()) + 3600
    )

    assert BrowserSessionRevocationStore(db_uri).is_revoked("sid-a")


def test_revoke_is_idempotent(store: BrowserSessionRevocationStore) -> None:
    expires_at = int(time.time()) + 3600
    store.revoke("sid-a", user_id="alice", expires_at=expires_at)
    store.revoke("sid-a", user_id="alice", expires_at=expires_at)

    assert _sids(store) == {"sid-a"}


def test_lapsed_rows_are_ignored_and_purged(
    store: BrowserSessionRevocationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows past the session's absolute expiry stop counting and are deleted."""
    now = int(time.time())
    store.revoke("sid-old", user_id="alice", expires_at=now + 10)
    monkeypatch.setattr(time, "time", lambda: now + 11)

    assert not store.is_revoked("sid-old")
    store.revoke("sid-new", user_id="bob", expires_at=now + 3600)
    assert _sids(store) == {"sid-new"}


def test_already_lapsed_session_is_not_recorded(store: BrowserSessionRevocationStore) -> None:
    store.revoke("sid-a", user_id="alice", expires_at=int(time.time()) - 1)

    assert _sids(store) == set()


def test_overlong_sid_is_never_stored(store: BrowserSessionRevocationStore) -> None:
    sid = "x" * 65
    store.revoke(sid, user_id="alice", expires_at=int(time.time()) + 3600)

    assert not store.is_revoked(sid)
    assert _sids(store) == set()


def test_lookup_is_scoped_to_the_workspace(store: BrowserSessionRevocationStore) -> None:
    """A logout in one workspace does not end a same-sid session in another."""
    expires_at = int(time.time()) + 3600
    with workspace_scope(1):
        store.revoke("sid-a", user_id="alice", expires_at=expires_at)

    with workspace_scope(1):
        assert store.is_revoked("sid-a")
    with workspace_scope(2):
        assert not store.is_revoked("sid-a")
    assert not store.is_revoked("sid-a")  # default workspace 0


def test_pruning_is_scoped_to_the_workspace(
    store: BrowserSessionRevocationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logout purges lapsed rows only in its own workspace."""
    now = int(time.time())
    for workspace_id in (1, 2):
        with workspace_scope(workspace_id):
            store.revoke("sid-old", user_id="alice", expires_at=now + 10)
    monkeypatch.setattr(time, "time", lambda: now + 11)

    with workspace_scope(1):
        store.revoke("sid-new", user_id="bob", expires_at=now + 3600)

    assert _rows(store) == {(1, "sid-new"), (2, "sid-old")}
    with workspace_scope(2):
        store.revoke("sid-other", user_id="carol", expires_at=now + 3600)
    assert _rows(store) == {(1, "sid-new"), (2, "sid-other")}


def _insert_rows(store: BrowserSessionRevocationStore, rows: dict[str, int]) -> None:
    """Insert rows directly, as earlier logouts would have: ``{sid: expires_at}``."""
    with store._session_immediate("test_insert_browser_session_revocations") as session:
        for sid, expires_at in rows.items():
            session.add(
                SqlBrowserSessionRevocation(
                    workspace_id=0,
                    sid=sid,
                    user_id="alice",
                    revoked_at=expires_at - 3600,
                    expires_at=expires_at,
                )
            )


def test_each_logout_purges_at_most_one_batch_and_the_backlog_drains(
    store: BrowserSessionRevocationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backlog of lapsed rows shrinks by one batch per logout; live rows stay."""
    monkeypatch.setattr(browser_session_store, "_PURGE_BATCH_SIZE", 3)
    now = int(time.time())
    lapsed = {f"sid-lapsed-{i}": now - 100 + i for i in range(7)}
    live = {"sid-live-a": now + 3600, "sid-live-b": now + 7200}
    _insert_rows(store, {**lapsed, **live})

    remaining = []
    for i in range(4):
        store.revoke(f"sid-new-{i}", user_id="bob", expires_at=now + 3600)
        remaining.append(_sids(store) & set(lapsed))

    assert [len(r) for r in remaining] == [4, 1, 0, 0]
    # Oldest first: the first logout removed the three earliest expiries.
    assert remaining[0] == {f"sid-lapsed-{i}" for i in range(3, 7)}
    assert set(live) <= _sids(store)
    assert {f"sid-new-{i}" for i in range(4)} <= _sids(store)


def test_purge_scan_is_bounded_by_the_expiry_index(
    store: BrowserSessionRevocationStore, db_uri: str
) -> None:
    """The lapsed-row selection is served in index order, with no sort or table scan."""
    if not db_uri.startswith("sqlite"):
        pytest.skip("query-plan shape asserted on SQLite")
    stmt = browser_session_store._lapsed_sids_query(0, int(time.time()))
    sql = str(stmt.compile(store._engine, compile_kwargs={"literal_binds": True}))
    with store._engine.connect() as conn:
        plan = " ".join(row[-1] for row in conn.execute(text(f"EXPLAIN QUERY PLAN {sql}")))

    assert "USING COVERING INDEX ix_browser_session_revocations_expires_at" in plan
    assert "workspace_id=?" in plan and "expires_at<?" in plan
    assert "TEMP B-TREE" not in plan


def test_overlong_user_id_is_cut_to_the_column_width(
    store: BrowserSessionRevocationStore,
) -> None:
    """The audit user id is bounded on every engine; the revocation still records."""
    store.revoke("sid-a", user_id="u" * 300, expires_at=int(time.time()) + 3600)

    assert store.is_revoked("sid-a")
    with store._session("test_select_browser_session_revocation_user") as session:
        stored = session.scalars(select(SqlBrowserSessionRevocation.user_id)).one()
    assert stored == "u" * 128


def test_revocation_expiring_after_2038_is_recorded(
    store: BrowserSessionRevocationStore,
) -> None:
    """A long configured lifetime can put the absolute expiry past the 32-bit limit."""
    store.revoke("sid-a", user_id="alice", expires_at=2**31 + 3600)

    assert store.is_revoked("sid-a")
