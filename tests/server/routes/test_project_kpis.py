"""Project KPIs: metric math, append-only revisions and who may write them."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from omnigent.entities.project_kpi import ProjectKpi
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.project_kpis import (
    DAY,
    KpiInputs,
    MetricSeries,
    MetricValue,
    SeriesItem,
    compute_metrics,
    hours_to_final_stage,
    kpi_rows,
    kpi_status,
    landed_items,
    landed_stage_index,
    load_series,
    nearest_rank,
    rework_items,
    series_value,
)
from omnigent.server.project_status import MemberTotals, StatusSources, status_period
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    _as_user,
    _bind_runner,
    multi_user_app,
    multi_user_client,
    project_agents,
    project_app,
    project_client,
)

HOUR = 3600


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


def _entry(stage_id: str, at: int, *, complete: bool = False, created: bool = False) -> dict:
    return {"stage_id": stage_id, "is_complete": complete, "at": at, "created": created}


NOW = _epoch("2026-03-10T12:00:00")
OLD = _epoch("2026-01-01T00:00:00")
ORDER = ["open", "in_progress", "code_review", "merged"]


def _window(series: MetricSeries, period) -> float | None:
    return series_value(series, period.since_epoch, period.until_epoch + 1).value


def test_rolling_24h_spend_takes_both_days_it_touches() -> None:
    # Spend is per UTC day; a 24h window starting mid-yesterday must keep yesterday's.
    spend = [
        SeriesItem(None, "root", "session", value=5_000_000, day_utc="2026-03-08"),
        SeriesItem(None, "root", "session", value=8_000_000, day_utc="2026-03-09"),
        SeriesItem(None, "root", "session", value=4_000_000, day_utc="2026-03-10"),
    ]
    shipped = [
        SeriesItem(_epoch("2026-03-09T15:00:00"), "t1", "ticket"),
        SeriesItem(_epoch("2026-03-10T01:00:00"), "t2", "ticket"),
    ]
    day = status_period("24h", NOW)
    assert _window(MetricSeries("daily_sum", items=spend), day) == 12.0
    assert _window(MetricSeries("ratio", items=[*spend, *shipped]), day) == 6.0
    week = status_period("7d", NOW)
    assert _window(MetricSeries("daily_sum", items=spend), week) == 17.0
    # A day-aligned bucket still takes only its own day.
    assert (
        series_value(
            MetricSeries("daily_sum", items=spend), NOW - 12 * HOUR, NOW - 12 * HOUR + DAY
        ).value
        == 4.0
    )


def test_rework_counts_backward_moves_and_reopens_in_the_window() -> None:
    period = status_period("7d", NOW)
    recent = NOW - HOUR
    histories = {
        "sent-back": [
            _entry("open", OLD, created=True),
            _entry("code_review", recent - 10),
            _entry("in_progress", recent),
            _entry("code_review", recent + 1),
            _entry("in_progress", recent + 2),
        ],
        "reopened": [_entry("merged", OLD, complete=True), _entry("open", recent)],
        "forward-only": [_entry("open", recent, created=True), _entry("merged", recent + 5)],
        "long-ago": [_entry("code_review", OLD), _entry("open", OLD + 1)],
    }
    stages = [{"id": stage, "is_complete": stage == "merged"} for stage in ORDER]
    rework = MetricSeries("distinct", items=rework_items(histories, stages))
    # A ticket sent back twice counts once.
    assert _window(rework, period) == 2
    assert _window(rework, status_period("all", NOW)) == 3


def test_rework_skips_routine_back_moves_and_names_each_reason() -> None:
    names = [
        ("open", "Pending"),
        ("in_progress", "Working"),
        ("code_review", "Review"),
        ("done", "Verified"),
        ("implemented", "Landed"),
        ("merged", "Merged"),
    ]
    stages = [{"id": i, "name": n, "is_complete": i == "merged"} for i, n in names]

    def moves(*stage_ids: str) -> list[dict]:
        first, *rest = stage_ids
        return [
            _entry(first, NOW - 100, created=True),
            *(
                _entry(stage, NOW - 99 + index, complete=stage == "merged")
                for index, stage in enumerate(rest)
            ),
        ]

    histories = {
        # Rebased after verification: back to Review for re-approval, then on. Routine.
        "rebased": moves("open", "in_progress", "code_review", "done", "code_review", "done"),
        # Landed before verified (the stage order changed later). Routine.
        "old-order": moves("open", "code_review", "implemented", "done", "merged"),
        "changes-requested": moves("open", "in_progress", "code_review", "in_progress"),
        "reopened": moves("open", "in_progress", "merged", "open"),
        "clean": moves("open", "in_progress", "code_review", "done", "implemented", "merged"),
    }
    items = rework_items(histories, stages)
    assert sorted((item.key, item.detail["reason"]) for item in items) == [
        ("changes-requested", "sent_back"),
        ("reopened", "reopened"),
    ]


def test_time_to_merged_uses_tickets_now_in_the_final_stage() -> None:
    period = status_period("7d", NOW)
    created = NOW - 10 * HOUR
    histories = {
        "fast": [_entry("open", created, created=True), _entry("merged", created + 2 * HOUR)],
        "slow": [_entry("open", created, created=True), _entry("merged", created + 8 * HOUR)],
        # Reopened after merging: not merged now.
        "reopened": [
            _entry("open", created, created=True),
            _entry("merged", created + HOUR),
            _entry("open", created + 3 * HOUR),
        ],
        # Merged before the window.
        "old": [_entry("open", OLD, created=True), _entry("merged", OLD + HOUR)],
        # No recorded creation: its age is unknown.
        "legacy": [_entry("open", created), _entry("merged", created + HOUR)],
    }
    samples, excluded = hours_to_final_stage(histories, "merged")
    median = series_value(MetricSeries("median", items=samples), period.since_epoch, NOW + 1)
    assert (median.value, median.sample_size) == (5.0, 2)
    assert sorted(entry.reason for entry in excluded) == [
        "Left the final stage after reaching it",
        "No recorded creation time",
    ]
    assert hours_to_final_stage(histories, None) == ([], [])
    assert nearest_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    assert nearest_rank([4], 0.9) == 4


SHA_A, SHA_B = "a" * 40, "b" * 40
LANDING = ["open", "code_review", "implemented", "merged"]


def test_changes_landed_counts_each_commit_once_when_it_reached_the_landed_stage() -> None:
    stages = [{"id": s, "is_complete": s == "merged"} for s in LANDING]
    recent = NOW - HOUR
    histories = {
        "landed": [_entry("open", OLD, created=True), _entry("implemented", recent)],
        # Its crossing is the latest one; moving on to Merged doesn't re-date it.
        "relanded": [
            _entry("open", OLD, created=True),
            _entry("implemented", OLD + 1),
            _entry("code_review", recent - 5),
            _entry("implemented", recent - 4),
            _entry("merged", recent - 3, complete=True),
        ],
        "same-commit": [_entry("open", OLD, created=True), _entry("merged", recent + 1)],
        "in-review": [_entry("open", OLD, created=True), _entry("code_review", recent)],
        "created-landed": [_entry("implemented", recent, created=True)],
        "old": [_entry("open", OLD, created=True), _entry("merged", OLD + 5, complete=True)],
    }
    labels = {
        "landed": ("implemented", {"verified": SHA_A.upper(), "approved": "c" * 40}),
        "relanded": ("merged", {"approved": SHA_B}),
        "same-commit": ("merged", {"verified": SHA_A}),
        "in-review": ("code_review", {"approved": "d" * 40}),
        "created-landed": ("implemented", {"verified": "e" * 40}),
        "old": ("merged", {"verified": "f" * 40}),
        "not-a-sha": ("merged", {"verified": "yes"}),
    }
    items, excluded = landed_items(histories, stages, {}, labels)
    assert sorted((item.key, item.detail["commit"]) for item in items) == [
        ("landed", SHA_A),
        ("old", "f" * 40),
        ("relanded", SHA_B),
    ]
    assert sorted((entry.item.key, entry.reason) for entry in excluded) == [
        ("created-landed", "Never moved into the landed stage"),
        ("same-commit", "Same commit as an earlier ticket"),
    ]
    landed = MetricSeries("distinct", items=items)
    assert _window(landed, status_period("7d", NOW)) == 2
    assert _window(landed, status_period("all", NOW)) == 3
    # Without a landing stage, landed work starts at the first completed stage.
    default = [{"id": s, "is_complete": s == "done"} for s in ("open", "review", "done")]
    assert landed_stage_index({}, default) == 2
    configured = {"landing": {"landed_stage": "code_review"}}
    assert landed_stage_index(configured, stages) == 1


def _inputs(spend, prs, histories, stages=None) -> KpiInputs:
    return KpiInputs(
        project=SimpleNamespace(id="p"),
        period=status_period("7d", NOW),
        sources=StatusSources(spend=spend, prs_merged=prs),
        stage_histories=histories,
        stages=stages or (lambda: [{"id": s, "is_complete": s == "merged"} for s in ORDER]),
    )


def test_compute_reads_only_what_the_kpis_need() -> None:
    created = NOW - 10 * HOUR

    def must_not_run():
        raise AssertionError("read data no KPI needs")

    spend = SimpleNamespace(spend=lambda project, period: MemberTotals(total=12345))
    histories = {
        "a": [
            _entry("open", created, created=True),
            _entry("merged", created + 3 * HOUR, complete=True),
        ],
        "b": [
            _entry("open", created, created=True),
            _entry("merged", created + HOUR, complete=True),
        ],
    }
    values = compute_metrics(
        ["spend_usd", "merged_prs", "tickets_shipped", "time_to_merged_p90_hours", "none"],
        _inputs(spend, None, lambda: histories),
    )
    assert values["spend_usd"] == MetricValue(123.45)
    # No PRs source on this server: unavailable, not zero.
    assert not values["merged_prs"].available
    assert values["tickets_shipped"] == MetricValue(2)
    assert values["time_to_merged_p90_hours"] == MetricValue(3.0, sample_size=2)
    assert "none" not in values
    lazy = _inputs(spend, None, must_not_run, must_not_run)
    assert set(compute_metrics(["spend_usd"], lazy)) == {"spend_usd"}


def test_trend_covers_seven_full_days_and_today() -> None:
    """The target check uses the 7 full days ending yesterday; today is shown separately."""
    today = _epoch("2026-03-10T00:00:00")
    yesterday = today - DAY
    eight_days_ago = today - 8 * DAY
    histories = {
        "y": [
            _entry("open", OLD, created=True),
            _entry("merged", yesterday + HOUR, complete=True),
        ],
        "t": [_entry("open", OLD, created=True), _entry("merged", NOW - HOUR, complete=True)],
        "o": [_entry("open", OLD, created=True), _entry("merged", eight_days_ago, complete=True)],
    }
    by_day = {"2026-03-03": 100, "2026-03-09": 250, "2026-03-10": 999, "2026-03-01": 5000}
    # (root session, day, micro-dollars, member) rows, as the spend source returns them.
    rows = [("s1", day, cents * 10_000, "user:local") for day, cents in by_day.items()]
    spend = SimpleNamespace(
        spend=lambda project, period: MemberTotals(total=1249),
        root_days=lambda project, period: rows,
    )
    merged = [
        SimpleNamespace(key="pr1", merged_at=yesterday + 5),
        SimpleNamespace(key="pr2", merged_at=None),
    ]
    prs = SimpleNamespace(merged_items=lambda project, since=None: merged)
    inputs = _inputs(spend, prs, lambda: histories)
    shipped = ProjectKpi(
        name="S", metric="tickets_shipped", why="w", target=2, direction="at_least"
    )
    budget = ProjectKpi(name="B", metric="spend_usd", why="w", target=3, direction="at_most")
    prs_kpi = ProjectKpi(name="P", metric="merged_prs", why="w", target=1, direction="at_least")
    tiles = kpi_rows(
        [shipped, budget, prs_kpi],
        load_series(["tickets_shipped", "spend_usd", "merged_prs"], inputs),
        inputs.period,
    )
    trend = tiles[0]["trend"]
    assert (trend["since_day_utc"], trend["until_day_utc"]) == ("2026-03-03", "2026-03-09")
    assert [day["day_utc"] for day in trend["days"]][-1] == "2026-03-10"
    assert len(trend["days"]) == 8
    assert (trend["last_7_days"], trend["today"], trend["status"]) == (1, 1, "missed")
    # The 7d window includes today, so it sees both tickets.
    assert tiles[0]["value"] == 2
    spend_trend = tiles[1]["trend"]
    assert (spend_trend["last_7_days"], spend_trend["today"]) == (3.5, 9.99)
    assert spend_trend["status"] == "missed"
    # The 7d window (from 2026-03-04) holds 2026-03-09 and today: $2.50 + $9.99.
    assert tiles[1]["value"] == 12.49
    # Undated merges count only without a lower bound, so not in the trend.
    assert (tiles[2]["trend"]["last_7_days"], tiles[2]["trend"]["status"]) == (1, "met")
    unmeasured = ProjectKpi(name="N", metric="none", why="w", not_measured_reason="Later.")
    assert kpi_rows([unmeasured], {}, inputs.period)[0]["trend"] is None


def test_trend_is_unavailable_when_a_source_has_only_totals() -> None:
    spend = SimpleNamespace(spend=lambda project, period: MemberTotals(total=500))
    inputs = _inputs(spend, None, dict)
    kpi = ProjectKpi(name="B", metric="spend_usd", why="w", target=3, direction="at_most")
    row = kpi_rows([kpi], load_series(["spend_usd"], inputs), inputs.period)[0]
    assert (row["value"], row["status"]) == (5.0, "missed")
    assert row["trend"]["status"] == "unavailable"
    assert {day["value"] for day in row["trend"]["days"]} == {None}


def test_status_compares_against_the_target_in_its_direction() -> None:
    def kpi(direction: str, target: float = 10) -> ProjectKpi:
        return ProjectKpi(
            name="K", metric="merged_prs", why="w", target=target, direction=direction
        )

    assert kpi_status(kpi("at_least"), MetricValue(10)) == "met"
    assert kpi_status(kpi("at_least"), MetricValue(9)) == "missed"
    assert kpi_status(kpi("at_most"), MetricValue(10)) == "met"
    assert kpi_status(kpi("at_most"), MetricValue(11)) == "missed"
    assert kpi_status(kpi("at_least"), MetricValue(None)) == "no_data"
    assert kpi_status(kpi("at_least"), MetricValue(None, available=False)) == "unavailable"
    unmeasured = ProjectKpi(name="N", metric="none", why="w", not_measured_reason="No source.")
    assert kpi_status(unmeasured, None) == "not_measured"


def test_definitions_are_validated() -> None:
    with pytest.raises(ValidationError, match="not_measured_reason"):
        ProjectKpi(name="N", metric="none", why="w")
    with pytest.raises(ValidationError, match="target and a direction"):
        ProjectKpi(name="N", metric="spend_usd", why="w")
    with pytest.raises(ValidationError, match="only for metric 'none'"):
        ProjectKpi(
            name="N",
            metric="spend_usd",
            why="w",
            target=1,
            direction="at_most",
            not_measured_reason="x",
        )


PRS = {"name": "PRs", "metric": "merged_prs", "why": "Shipping.", "target": 5}
SPEND = {
    "name": "Spend",
    "metric": "spend_usd",
    "why": "Budget.",
    "target": 100,
    "direction": "at_most",
}
NPS = {"name": "NPS", "metric": "none", "why": "Users.", "not_measured_reason": "No survey."}


async def test_owner_sets_kpis_as_append_only_revisions(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    path = f"/v1/projects/{created['id']}/kpis"
    empty = (await project_client.get(path)).json()
    assert (empty["revision"], empty["kpis"], empty["can_edit"]) == (0, [], True)
    assert {metric["id"] for metric in empty["metrics"]} >= {"merged_prs", "rework_tickets"}

    first = await project_client.put(
        path, json={"kpis": [{**PRS, "direction": "at_least"}, SPEND], "base_revision": 0}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["revision"] == 1
    assert [(kpi["name"], kpi["unit"], kpi["value"], kpi["status"]) for kpi in body["kpis"]] == [
        ("PRs", "count", 0, "missed"),
        ("Spend", "usd", 0, "met"),
    ]
    stale = await project_client.put(path, json={"kpis": [NPS], "base_revision": 0})
    assert stale.status_code == 409
    second = (
        await project_client.put(
            path, params={"window": "30d"}, json={"kpis": [NPS], "base_revision": 1}
        )
    ).json()
    assert (second["revision"], second["window"]) == (2, "30d")
    assert second["kpis"][0]["status"] == "not_measured"
    assert (await project_client.get(path)).json()["kpis"][0]["name"] == "NPS"

    history = (await project_client.get(f"{path}/revisions")).json()
    assert [row["revision"] for row in history["data"]] == [2, 1]
    assert [kpi["name"] for kpi in history["data"][1]["kpis"]] == ["PRs", "Spend"]
    older = (await project_client.get(f"{path}/revisions", params={"before": 2})).json()
    assert [row["revision"] for row in older["data"]] == [1]

    invalid = await project_client.put(path, json={"kpis": [PRS]})  # no direction
    assert invalid.status_code == 422
    duplicate = await project_client.put(path, json={"kpis": [SPEND, SPEND]})
    assert duplicate.status_code == 422


async def test_only_the_lead_writes_among_agents(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    path = f"/v1/projects/{created['id']}"
    lead_id = created["main_session_id"]
    lead = _bind_runner(db_uri, lead_id, "lead-secret")
    written = await project_client.put(f"{path}/kpis", json={"kpis": [SPEND]}, headers=lead)
    assert written.status_code == 200, written.text
    assert written.json()["updated_by_session_id"] == lead_id
    persona = (
        await project_client.post(f"{path}/identities", json={"name": "PM", "instructions": "y"})
    ).json()
    worker = _bind_runner(db_uri, persona["active_session_id"], "worker-secret")
    for headers in (worker, {ORCHESTRATION_SOURCE_HEADER: lead_id}):
        refused = await project_client.put(f"{path}/kpis", json={"kpis": []}, headers=headers)
        assert refused.status_code == 403
    # Agents still read them.
    read = await project_client.get(f"{path}/kpis", headers=worker)
    assert read.json()["revision"] == 1


async def test_members_read_kpis_but_only_the_owner_writes(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    owner, member, stranger = (
        _as_user("owner@example.com"),
        _as_user("member@example.com"),
        _as_user("stranger@example.com"),
    )
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Shared"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    await multi_user_client.post(
        f"{path}/shares", json={"user_id": "member@example.com"}, headers=owner
    )
    set_by_owner = await multi_user_client.put(
        f"{path}/kpis", json={"kpis": [SPEND]}, headers=owner
    )
    assert set_by_owner.json()["updated_by_user_id"] == "owner@example.com"
    read = (await multi_user_client.get(f"{path}/kpis", headers=member)).json()
    assert (read["revision"], read["can_edit"]) == (1, False)
    refused = await multi_user_client.put(f"{path}/kpis", json={"kpis": []}, headers=member)
    assert refused.status_code == 403
    assert (await multi_user_client.get(f"{path}/kpis", headers=stranger)).status_code == 404
    hidden = await multi_user_client.put(f"{path}/kpis", json={"kpis": []}, headers=stranger)
    assert hidden.status_code == 404


def test_drilldown_days_match_the_tile_and_list_what_is_left_out() -> None:
    """Each day's row count is the tile's day value; the total counts a ticket once."""
    from omnigent.server.project_status_items import drilldown

    today = _epoch("2026-03-10T00:00:00")
    yesterday = today - DAY
    histories = {
        # Done two days running: a row each day, counted once in the total, on its latest day.
        "twice": [
            _entry("open", OLD, created=True),
            _entry("merged", yesterday - DAY + HOUR, complete=True),
            _entry("open", yesterday),
            {**_entry("merged", yesterday + HOUR, complete=True), "user_id": "b@example.com"},
        ],
        "once": [_entry("open", OLD, created=True), _entry("merged", NOW - HOUR, complete=True)],
        "born-done": [_entry("merged", yesterday, complete=True, created=True)],
    }

    def member_of(entry):
        return f"user:{entry.get('user_id') or 'a@example.com'}"

    merged = [
        SimpleNamespace(key="https://git.example.com/o/r/pull/1", merged_at=yesterday + 5),
        SimpleNamespace(key="https://git.example.com/o/r/pull/2", merged_at=None),
    ]
    inputs = _inputs(
        None, SimpleNamespace(merged_items=lambda project, since=None: merged), lambda: histories
    )
    series = load_series(["tickets_shipped", "merged_prs"], inputs, member_of)
    shipped = ProjectKpi(
        name="S", metric="tickets_shipped", why="w", target=1, direction="at_least"
    )
    tile = kpi_rows([shipped], series, inputs.period)[0]["trend"]
    result = drilldown(series["tickets_shipped"], inputs.period, "trend")
    assert [(day["day_utc"], day["value"]) for day in result["days"]] == [
        (day["day_utc"], day["value"]) for day in tile["days"]
    ]
    assert all(day["items"] == day["value"] for day in result["days"])
    assert result["total"] == 2
    twice = [row for row in result["items"] if row["id"] == "twice"]
    assert [(row["day_utc"], row["counted_in_total"]) for row in twice] == [
        ("2026-03-09", True),
        ("2026-03-08", False),
    ]
    assert [(row["id"], row["reason"]) for row in result["excluded"]] == [
        ("born-done", "Created in a completed stage, never moved into it")
    ]
    # A member sees what's credited to them: the latest move decides.
    mine = drilldown(series["tickets_shipped"], inputs.period, "trend", "user:b@example.com")
    assert (mine["total"], {row["id"] for row in mine["items"]}) == (1, {"twice"})
    prs = drilldown(series["merged_prs"], inputs.period, "7d")
    assert prs["total"] == 1
    assert [row["reason"] for row in prs["excluded"]] == [
        "No merge time recorded, so it counts only in All time"
    ]
    everything = drilldown(series["merged_prs"], inputs.period, "all")
    assert (everything["total"], everything["excluded"]) == (2, [])


async def test_status_items_endpoint_lists_tickets_with_titles(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    owner, stranger = _as_user("owner@example.com"), _as_user("stranger@example.com")
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Items"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    stages = (await multi_user_client.get(f"{path}/tickets", headers=owner)).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    ticket = (
        await multi_user_client.post(f"{path}/tickets", json={"title": "Ship it"}, headers=owner)
    ).json()
    await multi_user_client.patch(
        f"{path}/tickets/{ticket['id']}", json={"stage_id": done}, headers=owner
    )
    response = await multi_user_client.get(
        f"{path}/status/items",
        params={"metric": "tickets_shipped", "range": "trend"},
        headers=owner,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["total"], body["unit"], len(body["days"])) == (1, "count", 8)
    assert body["days"][-1]["items"] == 1
    [row] = body["items"]
    assert (row["kind"], row["title"], row["member"]) == (
        "ticket",
        "Ship it",
        "user:owner@example.com",
    )
    mine = await multi_user_client.get(
        f"{path}/status/items",
        params={"metric": "tickets_shipped", "member": "user:someone@example.com"},
        headers=owner,
    )
    assert mine.json()["total"] == 0
    spend = await multi_user_client.get(
        f"{path}/status/items", params={"metric": "spend_usd"}, headers=owner
    )
    assert (spend.json()["total"], spend.json()["items"]) == (0, [])
    bad = await multi_user_client.get(
        f"{path}/status/items", params={"metric": "nope"}, headers=owner
    )
    assert bad.status_code == 422
    hidden = await multi_user_client.get(
        f"{path}/status/items", params={"metric": "tickets_shipped"}, headers=stranger
    )
    assert hidden.status_code == 404


def test_role_series_keeps_only_the_roles_tickets_and_sessions() -> None:
    from omnigent.server.project_kpis import UNAVAILABLE_SERIES, SeriesItem, role_series

    shipped = MetricSeries(
        "distinct",
        items=[SeriesItem(1, "t-mine", "ticket"), SeriesItem(1, "t-other", "ticket")],
    )
    merged = MetricSeries(
        "distinct",
        items=[
            SeriesItem(1, "ticket:t-mine", "ticket"),
            SeriesItem(
                1, "https://example.com/o/r/pull/7", "pull_request", claimed_by=("t-mine",)
            ),
            SeriesItem(
                1, "https://example.com/o/r/pull/8", "pull_request", claimed_by=("s-mine",)
            ),
            SeriesItem(
                1, "https://example.com/o/r/pull/9", "pull_request", claimed_by=("t-other",)
            ),
        ],
    )
    spend = MetricSeries(
        "daily_sum",
        items=[
            SeriesItem(None, "s-mine", "session", value=5, day_utc="2026-11-24"),
            SeriesItem(None, "s-other", "session", value=9, day_utc="2026-11-24"),
        ],
    )
    total_only = MetricSeries("distinct", dated=False, window_value=MetricValue(3))
    narrowed = role_series(
        {"tickets_shipped": shipped, "merged_prs": merged, "spend_usd": spend, "x": total_only},
        {"t-mine"},
        {"s-mine"},
    )
    assert [item.key for item in narrowed["tickets_shipped"].items] == ["t-mine"]
    assert [item.key for item in narrowed["merged_prs"].items] == [
        "ticket:t-mine",
        "https://example.com/o/r/pull/7",
        "https://example.com/o/r/pull/8",
    ]
    assert [item.key for item in narrowed["spend_usd"].items] == ["s-mine"]
    assert narrowed["x"] is UNAVAILABLE_SERIES


async def test_a_role_has_its_own_kpis_set_by_the_owner_or_lead(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    path = f"/v1/projects/{created['id']}"
    pm = (
        await project_client.post(f"{path}/identities", json={"name": "PM", "instructions": "y"})
    ).json()
    role_path = f"{path}/identities/{pm['id']}/kpis"
    empty = (await project_client.get(role_path)).json()
    assert (empty["revision"], empty["identity_id"], empty["kpis"]) == (0, pm["id"], [])

    lead = _bind_runner(db_uri, created["main_session_id"], "lead-secret")
    written = await project_client.put(role_path, json={"kpis": [SPEND]}, headers=lead)
    assert written.status_code == 200, written.text
    assert written.json()["revision"] == 1
    worker = _bind_runner(db_uri, pm["active_session_id"], "worker-secret")
    refused = await project_client.put(role_path, json={"kpis": []}, headers=worker)
    assert refused.status_code == 403
    # Role and project sets are versioned separately.
    assert (await project_client.get(f"{path}/kpis")).json()["revision"] == 0
    missing = await project_client.get(f"{path}/identities/{'0' * 32}/kpis")
    assert missing.status_code == 404

    shipped_kpi = {
        "name": "Shipped",
        "metric": "tickets_shipped",
        "why": "Throughput.",
        "target": 1,
        "direction": "at_least",
    }
    for title, labels in (("Mine", [{"key": "assignee", "value": "pm"}]), ("Other", [])):
        ticket = (
            await project_client.post(f"{path}/tickets", json={"title": title, "labels": labels})
        ).json()
        moved = await project_client.patch(
            f"{path}/tickets/{ticket['id']}", json={"stage_id": "done"}
        )
        assert moved.status_code == 200, moved.text
    await project_client.put(role_path, json={"kpis": [shipped_kpi]})
    await project_client.put(f"{path}/kpis", json={"kpis": [shipped_kpi]})
    role_value = (await project_client.get(role_path)).json()["kpis"][0]["value"]
    project_value = (await project_client.get(f"{path}/kpis")).json()["kpis"][0]["value"]
    assert (role_value, project_value) == (1, 2)


async def test_summary_and_insights_count_changes_landed_only_once_one_landed(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    owner = _as_user("owner@example.com")
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Landed"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    stages = (await multi_user_client.get(f"{path}/tickets", headers=owner)).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])

    async def landed() -> tuple:
        summary = await multi_user_client.get(f"{path}/status/summary", headers=owner)
        insights = await multi_user_client.get(f"{path}/telemetry", headers=owner)
        return (
            summary.json()["totals"]["changes_landed"],
            insights.json()["throughput"]["changes_landed"],
        )

    first = (
        await multi_user_client.post(f"{path}/tickets", json={"title": "No commit"}, headers=owner)
    ).json()
    await multi_user_client.patch(
        f"{path}/tickets/{first['id']}", json={"stage_id": done}, headers=owner
    )
    assert await landed() == (None, None)
    second = (
        await multi_user_client.post(f"{path}/tickets", json={"title": "Landed"}, headers=owner)
    ).json()
    await multi_user_client.patch(
        f"{path}/tickets/{second['id']}",
        json={"labels": [{"key": "verified", "value": SHA_A}], "stage_id": done},
        headers=owner,
    )
    assert await landed() == (1, 1)
    items = await multi_user_client.get(
        f"{path}/status/items", params={"metric": "changes_landed"}, headers=owner
    )
    [row] = items.json()["items"]
    assert (row["title"], row["detail"]) == ("Landed", {"commit": SHA_A})


def test_trend_buckets_and_the_open_ticket_burndown() -> None:
    from omnigent.server.project_status_trends import buckets, open_tickets_at

    size, week = buckets(status_period("7d", NOW), None)
    assert (size, week[0].day_utc, len(week)) == ("day", "2026-03-04", 7)
    assert week[-1].until == NOW + 1
    size, long = buckets(status_period("all", NOW), "2025-01-01")
    assert size == "week" and long[-1].day_utc == "2026-03-04"
    assert long[0].since <= _epoch("2025-01-01T00:00:00") < long[0].until
    histories = {
        "done": [_entry("open", 10, created=True), _entry("merged", 20, complete=True)],
        "reopened": [
            _entry("open", 10, created=True),
            _entry("merged", 15, complete=True),
            _entry("open", 30),
        ],
        "late": [_entry("open", 40, created=True)],
    }
    assert open_tickets_at(histories, [5, 11, 16, 21, 31, 41]) == [0, 2, 1, 0, 1, 2]


async def test_trends_endpoint_matches_the_drilldown_day_by_day(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    owner = _as_user("owner@example.com")
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "Trends"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    stages = (await multi_user_client.get(f"{path}/tickets", headers=owner)).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    for index in range(3):
        ticket = (
            await multi_user_client.post(
                f"{path}/tickets", json={"title": f"T{index}"}, headers=owner
            )
        ).json()
        if index:
            labels = [{"key": "verified", "value": f"{index}" * 40}]
            await multi_user_client.patch(
                f"{path}/tickets/{ticket['id']}",
                json={"labels": labels, "stage_id": done},
                headers=owner,
            )
    body = (
        await multi_user_client.get(
            f"{path}/status/trends", params={"window": "7d"}, headers=owner
        )
    ).json()
    assert (body["bucket"], len(body["days"])) == ("day", 7)
    today = body["days"][-1]
    assert (today["tickets_filed"], today["tickets_finished"], today["open_tickets"]) == (3, 2, 1)
    assert today["changes_landed"] == 2
    for metric, field in (("tickets_shipped", "tickets_finished"), ("changes_landed", None)):
        items = await multi_user_client.get(
            f"{path}/status/items", params={"metric": metric, "range": "7d"}, headers=owner
        )
        drill = [day["value"] for day in items.json()["days"]]
        assert drill == [day[field or metric] for day in body["days"]]


def test_telemetry_splits_rework_by_reason_within_the_window() -> None:
    from omnigent.server.routes.project_telemetry import rework_split

    def move(key: str, reason: str, at: int) -> SeriesItem:
        return SeriesItem(at, key, "ticket", detail={"reason": reason})

    series = MetricSeries(
        "distinct",
        items=[
            move("a", "reopened", NOW - HOUR),
            move("a", "reopened", NOW - 2 * HOUR),
            move("b", "sent_back", NOW - HOUR),
            move("c", "sent_back", OLD),
        ],
    )
    assert rework_split(series, status_period("7d", NOW)) == {
        "rework_reopened_tickets": 1,
        "rework_sent_back_tickets": 1,
    }
