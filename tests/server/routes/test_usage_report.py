"""Tests for the ``GET /v1/usage`` report builder and its helpers."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError, StaleCursorError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.feature_flags import FeatureFlags
from omnigent.server.routes.usage import (
    MAX_SESSION_PAGE_SIZE,
    _build_usage_report,
    _day_offset,
    _session_cost,
    _session_models,
    create_usage_router,
)
from omnigent.server.schemas import UsageReport
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
    # The legacy CLI report remains available while page-only details stay dark.
    assert report.daily_costs == []


def test_build_usage_report_includes_page_details_when_enabled(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    store.add_daily_cost(RESERVED_USER_LOCAL, "2026-07-22", 1.25)
    session_id = _add_session(
        store,
        monkeypatch,
        ts=1_784_678_400,
        cost=1.25,
        by_model={"model-a": {"total_cost_usd": 1.25}},
        title="page session",
    )
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: 1_784_678_400)
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_session_harness",
        lambda _conv: "codex-native",
    )
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_llm_model",
        lambda _conv: "model-a",
    )

    report = _build_usage_report(store, None, include_page_details=True)

    assert [(item.day, item.cost_usd) for item in report.daily_costs] == [("2026-07-22", 1.25)]
    session = next(item for item in report.sessions if item.id == session_id)
    assert session.harness == "codex-native"
    assert session.llm_model == "model-a"


def test_build_usage_report_skips_page_resolution_while_disabled(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    _add_session(
        store,
        monkeypatch,
        ts=1_700_000_000,
        cost=1.0,
        by_model={},
        title="legacy session",
    )

    def _unexpected(_conv: object) -> str:
        pytest.fail("page-only metadata was resolved while usage_page was off")

    monkeypatch.setattr("omnigent.server.routes.usage._resolve_session_harness", _unexpected)
    monkeypatch.setattr("omnigent.server.routes.usage._resolve_llm_model", _unexpected)

    report = _build_usage_report(store, None)

    assert report.daily_costs == []
    assert report.sessions[0].harness is None
    assert report.sessions[0].llm_model is None


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


def test_build_usage_report_first_page_computes_breakdowns(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    _add_session(
        store,
        monkeypatch,
        ts=1_700_000_000,
        cost=1.25,
        by_model={"model-a": {"total_cost_usd": 1.25}},
        title="s1",
    )
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: 1_700_000_000)
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_session_harness",
        lambda _conv: "codex-native",
    )
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_llm_model",
        lambda _conv: "model-a",
    )

    # The web page always requests a bounded first page; the breakdowns ride it.
    report = _build_usage_report(store, None, include_page_details=True, limit=50)

    assert report.harness_breakdown == {"codex-native": 1.25}
    assert report.model_breakdown == {"model-a": 1.25}


def test_build_usage_report_breakdowns_span_all_sessions_not_just_the_page(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    for i, cost in enumerate((1.0, 2.0, 4.0)):
        _add_session(
            store,
            monkeypatch,
            ts=now - i * _DAY,
            cost=cost,
            by_model={"model-a": {"total_cost_usd": cost}},
            title=f"s{i}",
        )
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_session_harness",
        lambda _conv: "codex-native",
    )
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_llm_model",
        lambda _conv: "model-a",
    )

    # One session fits on the page, but with no date filter the breakdowns still
    # sum every session the account owns — the page no longer holds them to add
    # up.
    report = _build_usage_report(store, None, include_page_details=True, limit=1)

    assert len(report.sessions) == 1
    assert report.sessions_has_more is True
    assert report.harness_breakdown == {"codex-native": 7.0}
    assert report.model_breakdown == {"model-a": 7.0}


def test_build_usage_report_breakdowns_respect_date_range(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    _add_session(
        store,
        monkeypatch,
        ts=now - 2 * _DAY,
        cost=1.0,
        by_model={"model-a": {"total_cost_usd": 1.0}},
        title="old",
    )
    _add_session(
        store,
        monkeypatch,
        ts=now,
        cost=4.0,
        by_model={"model-a": {"total_cost_usd": 4.0}},
        title="new",
    )
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_session_harness",
        lambda _conv: "codex-native",
    )

    # The breakdowns follow the selected range: a window that excludes the older
    # session sums only the in-window spend, matching the session list.
    report = _build_usage_report(
        store, None, include_page_details=True, limit=50, updated_at_min=now
    )

    assert [s.title for s in report.sessions] == ["new"]
    assert report.harness_breakdown == {"codex-native": 4.0}
    assert report.model_breakdown == {"model-a": 4.0}


def test_build_usage_report_breakdowns_bucket_unknown_harness(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    _add_session(
        store,
        monkeypatch,
        ts=1_700_000_000,
        cost=2.5,
        by_model={"model-a": {"total_cost_usd": 2.5}},
        title="no-harness",
    )
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: 1_700_000_000)
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_session_harness",
        lambda _conv: None,
    )

    # A session with no resolved harness still contributes its spend under an
    # "Unknown" bucket, so the chart total matches the account total.
    report = _build_usage_report(store, None, include_page_details=True, limit=50)

    assert report.harness_breakdown == {"Unknown": 2.5}
    assert report.model_breakdown == {"model-a": 2.5}


def test_build_usage_report_cursor_pages_skip_breakdowns(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    _add_session(
        store,
        monkeypatch,
        ts=now - _DAY,
        cost=1.0,
        by_model={"model-a": {"total_cost_usd": 1.0}},
        title="older",
    )
    _add_session(
        store,
        monkeypatch,
        ts=now,
        cost=2.0,
        by_model={"model-a": {"total_cost_usd": 2.0}},
        title="newer",
    )
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_session_harness",
        lambda _conv: "codex-native",
    )
    monkeypatch.setattr(
        "omnigent.server.routes.usage._resolve_llm_model",
        lambda _conv: "model-a",
    )

    first = _build_usage_report(store, None, include_page_details=True, limit=1)
    assert first.sessions_has_more
    assert first.harness_breakdown == {"codex-native": 3.0}

    second = _build_usage_report(
        store, None, include_page_details=True, limit=1, after=first.sessions_last_id
    )

    # Cursor pages carry sessions only: the web UI reads breakdowns from the
    # first page, so later pages neither recompute nor return them.
    assert [s.title for s in second.sessions] == ["older"]
    assert second.harness_breakdown == {}
    assert second.model_breakdown == {}


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


def test_build_usage_report_pages_and_filters_sessions_by_updated_at(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    ids = [
        _add_session(store, monkeypatch, ts=now - 2 * _DAY, cost=1.0, by_model={}, title="oldest"),
        _add_session(store, monkeypatch, ts=now - _DAY, cost=1.0, by_model={}, title="middle"),
        _add_session(store, monkeypatch, ts=now, cost=1.0, by_model={}, title="newest"),
    ]
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)

    first = _build_usage_report(store, None, limit=2)
    assert [s.title for s in first.sessions] == ["newest", "middle"]
    assert first.sessions_has_more is True
    assert first.sessions_last_id == ids[1]

    last = _build_usage_report(store, None, limit=2, after=first.sessions_last_id)
    assert [s.title for s in last.sessions] == ["oldest"]
    assert last.sessions_has_more is False

    # Inclusive bounds on last activity, applied before paging.
    window = _build_usage_report(store, None, updated_at_min=now - _DAY, updated_at_max=now - _DAY)
    assert [s.title for s in window.sessions] == ["middle"]
    assert window.sessions_has_more is False


def _usage_client(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> TestClient:
    """A client for the usage route whose report builder records its keyword arguments."""

    def fake_build(store: object, user_id: object, **kwargs: object) -> UsageReport:
        calls.append(kwargs)
        return UsageReport()

    monkeypatch.setattr("omnigent.server.routes.usage._build_usage_report", fake_build)
    app = FastAPI()
    app.include_router(
        create_usage_router(object(), feature_flags=FeatureFlags(frozenset())),  # type: ignore[arg-type]
        prefix="/v1",
    )
    return TestClient(app)


def test_get_usage_clamps_limit_and_converts_date_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    client = _usage_client(monkeypatch, calls)

    assert client.get("/v1/usage").status_code == 200
    assert client.get("/v1/usage?limit=1000&after=conv_x").status_code == 200
    assert client.get("/v1/usage?limit=0&since=2026-07-01&until=2026-07-02").status_code == 200

    # No ``limit`` keeps the unbounded legacy contract; the web page opts in.
    assert calls[0]["limit"] is None
    assert calls[0]["after"] is None
    assert calls[1]["limit"] == MAX_SESSION_PAGE_SIZE
    assert calls[1]["after"] == "conv_x"
    assert calls[2]["limit"] == 1
    # 2026-07-01T00:00:00Z .. 2026-07-02T23:59:59Z
    assert calls[2]["updated_at_min"] == 1_782_864_000
    assert calls[2]["updated_at_max"] == 1_783_036_799


@pytest.mark.parametrize("query", ["since=07/01/2026", "until=2026-13-01", "since=yesterday"])
def test_get_usage_rejects_malformed_dates(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    calls: list[dict[str, object]] = []
    client = _usage_client(monkeypatch, calls)

    resp = client.get(f"/v1/usage?{query}")

    assert resp.status_code == 422, resp.text
    assert "YYYY-MM-DD" in resp.json()["detail"]
    assert calls == []


def test_get_usage_maps_stale_cursor_to_http_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A builder stale cursor surfaces over HTTP as 400 ``stale_cursor``."""

    def raise_stale(store: object, user_id: object, **kwargs: object) -> UsageReport:
        raise StaleCursorError("conv_gone")

    monkeypatch.setattr("omnigent.server.routes.usage._build_usage_report", raise_stale)
    app = FastAPI()
    app.include_router(
        create_usage_router(object(), feature_flags=FeatureFlags(frozenset())),  # type: ignore[arg-type]
        prefix="/v1",
    )

    # Mirror create_app's OmnigentError handler so the test exercises the real
    # error-code -> HTTP-status mapping the paginated client recovers from.
    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    resp = TestClient(app).get("/v1/usage?after=conv_gone")

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "stale_cursor"


def test_build_usage_report_stale_client_cursor_raises(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    _add_session(store, monkeypatch, ts=now - _DAY, cost=1.0, by_model={}, title="older")
    newest = _add_session(store, monkeypatch, ts=now, cost=1.0, by_model={}, title="newest")
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)
    first = _build_usage_report(store, None, limit=1)
    assert first.sessions_last_id == newest

    assert asyncio.run(store.delete_conversation(newest))

    # A deleted client cursor surfaces as StaleCursorError, not an empty page.
    with pytest.raises(StaleCursorError):
        _build_usage_report(store, None, limit=1, after=newest)


def test_build_usage_report_unbounded_returns_every_session(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    for i in range(3):
        _add_session(store, monkeypatch, ts=now - i * _DAY, cost=1.0, by_model={}, title=f"s{i}")
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)

    # No ``limit`` keeps the pre-pagination contract: every session, one report.
    report = _build_usage_report(store, None)

    assert len(report.sessions) == 3
    assert report.sessions_has_more is False


def test_build_usage_report_unbounded_walk_restarts_on_stale_cursor(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    now = 1_700_000_000
    for i in range(3):
        _add_session(store, monkeypatch, ts=now - i * _DAY, cost=1.0, by_model={}, title=f"s{i}")
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: now)

    from omnigent.server.routes import usage as usage_module

    real_page = usage_module._list_conversation_page
    calls = {"n": 0}

    def flaky_page(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise StaleCursorError("conv_gone")
        return real_page(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(usage_module, "_list_conversation_page", flaky_page)

    # The unbounded legacy walk restarts on a cursor that vanished mid-walk
    # instead of surfacing it, so the caller still gets every surviving session.
    report = _build_usage_report(store, None)

    assert len(report.sessions) == 3
    assert report.sessions_has_more is False
    assert calls["n"] >= 2  # the stale first attempt plus the restart
