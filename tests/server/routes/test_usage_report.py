"""Tests for the ``GET /v1/usage`` report builder and its helpers."""

from __future__ import annotations

import pytest

from omnigent.server.routes.sessions import (
    _build_usage_report,
    _primary_model,
    _usage_window_starts,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_DAY = 86_400
# Agent ids are stored as 16-byte uuids, so tests use a valid 32-char hex id.
_AGENT_ID = "0123456789abcdef0123456789abcdef"


def test_usage_window_starts() -> None:
    now = 1_700_000_000
    since_24h, since_7d, since_30d = _usage_window_starts(now)
    assert since_24h == now - _DAY
    assert since_7d == now - 7 * _DAY
    assert since_30d == now - 30 * _DAY


def test_primary_model_single() -> None:
    by_model = {"claude-opus-4-8": {"total_cost_usd": 1.0}}
    assert _primary_model(by_model) == "claude-opus-4-8"


def test_primary_model_keeps_full_id() -> None:
    # The full model id is shown verbatim — no prefix/suffix rewriting.
    by_model = {"provider.foo-4[1m]": {"total_cost_usd": 1.0}}
    assert _primary_model(by_model) == "provider.foo-4[1m]"


def test_primary_model_collapses_identical_alias_buckets() -> None:
    # The same spend recorded under two names is one model: collapse to the
    # shortest id (picked verbatim) with no spurious "+N".
    by_model = {
        "system.ai.claude-opus-4-8[1m]": {"total_cost_usd": 2.0},
        "claude-opus-4-8": {"total_cost_usd": 2.0},
    }
    assert _primary_model(by_model) == "claude-opus-4-8"


def test_primary_model_multiple_ranks_by_cost() -> None:
    by_model = {
        "claude-sonnet-5": {"total_cost_usd": 1.0},
        "claude-opus-4-8": {"total_cost_usd": 3.0},
    }
    assert _primary_model(by_model) == "claude-opus-4-8 +1"


@pytest.mark.parametrize("value", [None, {}, "not-a-dict", 5])
def test_primary_model_missing(value: object) -> None:
    assert _primary_model(value) is None


def _add_session(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ts: int,
    cost: float,
    by_model: dict[str, dict[str, float]],
    title: str,
) -> str:
    # create_conversation stamps updated_at from now_epoch; pin it to place the
    # session in a specific window. set_session_usage does not touch updated_at.
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: ts,
    )
    conv = store.create_conversation(title=title, agent_id=_AGENT_ID)
    store.set_session_usage(conv.id, {"total_cost_usd": cost, "by_model": by_model})
    return conv.id


def test_build_usage_report_windows_and_totals(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000

    recent = _add_session(
        store,
        monkeypatch,
        ts=now - 3600,
        cost=1.0,
        by_model={"claude-opus-4-8": {"total_cost_usd": 1.0}},
        title="recent",
    )
    week = _add_session(
        store,
        monkeypatch,
        ts=now - 3 * _DAY,
        cost=2.0,
        by_model={"gpt-5.5": {"total_cost_usd": 2.0}},
        title="week",
    )
    old = _add_session(
        store,
        monkeypatch,
        ts=now - 40 * _DAY,
        cost=4.0,
        by_model={
            "claude-opus-4-8": {"total_cost_usd": 3.0},
            "claude-sonnet-5": {"total_cost_usd": 1.0},
        },
        title="old",
    )

    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    report = _build_usage_report(store, None)

    assert report.cost_last_24h == 1.0
    assert report.cost_last_7d == 3.0
    assert report.cost_last_30d == 3.0
    assert report.total_cost_usd == 7.0

    # Newest activity first, subtree cost + primary model per session.
    assert [s.id for s in report.sessions] == [recent, week, old]
    assert [s.cost_usd for s in report.sessions] == [1.0, 2.0, 4.0]
    assert [s.model for s in report.sessions] == [
        "claude-opus-4-8",
        "gpt-5.5",
        "claude-opus-4-8 +1",
    ]


def test_build_usage_report_empty(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    report = _build_usage_report(store, None)
    assert report.sessions == []
    assert report.cost_last_24h == 0.0
    assert report.cost_last_7d == 0.0
    assert report.cost_last_30d == 0.0
    assert report.total_cost_usd == 0.0


def test_build_usage_report_unpriced_session(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: now,
    )
    # A session with no recorded usage: cost falls back to 0.0, model None.
    conv = store.create_conversation(title="bare", agent_id=_AGENT_ID)

    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    report = _build_usage_report(store, None)

    bare = next(s for s in report.sessions if s.id == conv.id)
    assert bare.cost_usd == 0.0
    assert bare.model is None
