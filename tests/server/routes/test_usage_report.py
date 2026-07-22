"""Tests for the ``GET /v1/usage`` report builder and its helpers."""

from __future__ import annotations

import pytest

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes.usage import (
    _build_usage_report,
    _day_offset,
    _session_cost,
    _session_models,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_DAY = 86_400
# Agent ids are stored as 16-byte uuids, so tests use a valid 32-char hex id.
_AGENT_ID = "0123456789abcdef0123456789abcdef"


def test_day_offset() -> None:
    # Inclusive windows: today + the 6 prior days = a 7-day window.
    assert _day_offset("2026-07-22", days=6) == "2026-07-16"
    assert _day_offset("2026-07-22", days=29) == "2026-06-23"
    # Crosses a month boundary correctly.
    assert _day_offset("2026-07-01", days=1) == "2026-06-30"


def test_session_cost_priced() -> None:
    assert _session_cost({"total_cost_usd": 2.5}) == 2.5


def test_session_cost_unpriced_or_malformed() -> None:
    # Absent key (unpriced) and malformed values both read as 0.0.
    assert _session_cost({}) == 0.0
    assert _session_cost({"total_cost_usd": "oops"}) == 0.0


def test_session_models_faithful_no_collapse() -> None:
    # Per-model costs are projected verbatim — no alias collapsing, no
    # requirement that they sum to the session total (matches the web UI).
    usage = {
        "total_cost_usd": 14.03,
        "by_model": {
            "system.ai.claude-opus-4-8[1m]": {"total_cost_usd": 14.03},
            "system.ai.claude-sonnet-4-6[1m]": {"total_cost_usd": 12.36},
        },
    }
    assert _session_models(usage) == {
        "system.ai.claude-opus-4-8[1m]": 14.03,
        "system.ai.claude-sonnet-4-6[1m]": 12.36,
    }


def test_session_models_omits_unpriced_and_malformed() -> None:
    usage = {
        "by_model": {
            "claude-opus-4-8": {"total_cost_usd": 1.0},
            "unpriced-model": {"input_tokens": 100},  # no cost key -> omitted
            "bad-model": {"total_cost_usd": "nan-ish"},  # malformed -> omitted
        }
    }
    assert _session_models(usage) == {"claude-opus-4-8": 1.0}


@pytest.mark.parametrize("value", [None, {}, "not-a-dict", 5])
def test_session_models_missing(value: object) -> None:
    assert _session_models({"by_model": value}) == {}
    assert _session_models({}) == {}


def _add_session(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ts: int,
    cost: float,
    by_model: dict[str, dict[str, float]],
    title: str,
) -> str:
    # create_conversation stamps updated_at from now_epoch; pin it so the
    # session lands at a specific time. set_session_usage does not touch it.
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: ts,
    )
    conv = store.create_conversation(title=title, agent_id=_AGENT_ID)
    store.set_session_usage(conv.id, {"total_cost_usd": cost, "by_model": by_model})
    return conv.id


def test_build_usage_report_summary_from_daily_rollup(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    # The summary windows are sourced from the per-user daily rollup, NOT from
    # each session's last-activity time — so record spend directly on the
    # rollup, attributed to calendar days relative to "today".
    store.add_daily_cost(RESERVED_USER_LOCAL, "2026-07-22", 1.0)  # today
    store.add_daily_cost(RESERVED_USER_LOCAL, "2026-07-18", 2.0)  # within 7d
    store.add_daily_cost(RESERVED_USER_LOCAL, "2026-07-01", 4.0)  # within 30d
    store.add_daily_cost(RESERVED_USER_LOCAL, "2026-05-01", 8.0)  # older, all-time only

    # 1_784_678_400 == 2026-07-22T00:00:00Z. usage._utc_today reads now_epoch
    # from omnigent.db.utils, so patch it there.
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: 1_784_678_400)
    report = _build_usage_report(store, None)

    assert report.cost_today == 1.0
    assert report.cost_last_7d == 3.0  # today + 2026-07-18
    assert report.cost_last_30d == 7.0  # + 2026-07-01
    assert report.total_cost_usd == 15.0  # + 2026-05-01


def test_build_usage_report_sessions_detail(
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
    older = _add_session(
        store,
        monkeypatch,
        ts=now - 3 * _DAY,
        cost=6.02,
        # Multi-model session whose per-model costs deliberately do NOT sum to
        # the session total (native cumulative attribution) — shown faithfully.
        by_model={
            "claude-opus-4-8": {"total_cost_usd": 6.02},
            "system.ai.claude-opus-4-8[1m]": {"total_cost_usd": 13.58},
        },
        title="older",
    )

    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    report = _build_usage_report(store, None)

    # Newest activity first; authoritative session cost + faithful per-model map.
    assert [s.id for s in report.sessions] == [recent, older]
    assert [s.cost_usd for s in report.sessions] == [1.0, 6.02]
    assert report.sessions[0].models == {"claude-opus-4-8": 1.0}
    assert report.sessions[1].models == {
        "claude-opus-4-8": 6.02,
        "system.ai.claude-opus-4-8[1m]": 13.58,
    }


def test_build_usage_report_empty(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    report = _build_usage_report(store, None)
    assert report.sessions == []
    assert report.cost_today == 0.0
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
    # A session with no recorded usage: cost falls back to 0.0, no models.
    conv = store.create_conversation(title="bare", agent_id=_AGENT_ID)

    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    report = _build_usage_report(store, None)

    bare = next(s for s in report.sessions if s.id == conv.id)
    assert bare.cost_usd == 0.0
    assert bare.models == {}


def test_sum_daily_cost_range(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    store.add_daily_cost("alice", "2026-07-01", 1.0)
    store.add_daily_cost("alice", "2026-07-10", 2.0)
    store.add_daily_cost("alice", "2026-07-20", 4.0)
    store.add_daily_cost("bob", "2026-07-20", 100.0)  # other user, excluded

    assert store.sum_daily_cost("alice", "2026-07-10") == 6.0  # 10th + 20th
    assert store.sum_daily_cost("alice", "0000-00-00") == 7.0  # all-time
    assert store.sum_daily_cost("alice", "2026-08-01") == 0.0  # nothing in range
    assert store.sum_daily_cost("nobody", "0000-00-00") == 0.0
