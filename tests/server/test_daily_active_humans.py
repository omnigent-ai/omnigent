"""Daily active people (weekday mean) and spend per finished ticket, as KPI metrics."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.project_kpis import (
    MetricSeries,
    SeriesItem,
    aggregate,
    series_value,
)
from omnigent.stores.project_ticket_store import ProjectTicketStore
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    _as_user,
    multi_user_app,
    multi_user_client,
    project_agents,
    project_app,
    project_client,
)


def _at(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=UTC).timestamp())


def _person(user: str, day: str, hour: int = 12) -> SeriesItem:
    return SeriesItem(_at(day, hour), user, "person")


# 2026-03-02 is a Monday; 2026-03-07 and 03-08 are the weekend.
WEEK = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]


def test_weekday_mean_skips_weekends_and_counts_quiet_weekdays_as_zero() -> None:
    items = [
        _person("ann", "2026-03-02"),
        _person("ann", "2026-03-02", 15),  # Twice in a day still counts once.
        _person("bob", "2026-03-02"),
        _person("ann", "2026-03-03"),
        # Nobody on Wednesday to Friday.
        _person("cat", "2026-03-07"),  # Saturday: ignored.
        _person("dan", "2026-03-08"),  # Sunday: ignored.
    ]
    series = MetricSeries("weekday_mean", items=items)
    # Mon 2 + Tue 1 + Wed..Fri 0 over five weekdays, across the whole Mon-Sun week.
    week = series_value(series, _at("2026-03-02", 0), _at("2026-03-09", 0))
    assert (week.value, week.sample_size) == (0.6, 5)
    # A weekend on its own has no weekdays to average.
    weekend = series_value(series, _at("2026-03-07", 0), _at("2026-03-09", 0))
    assert (weekend.value, weekend.sample_size) == (None, 0)
    # One drill-down day is that day's distinct people.
    assert aggregate("weekday_mean", items[:3]).value == 2


def test_rolling_seven_days_always_cover_five_weekdays() -> None:
    items = [_person(f"user{index}", day) for index, day in enumerate(WEEK)] + [
        _person("weekend", "2026-03-07"),
        _person("early", "2026-03-09"),
    ]
    series = MetricSeries("weekday_mean", items=items)
    # Wed 03-04 through Tue 03-10: Wed, Thu, Fri, Mon, Tue.
    rolling = series_value(series, _at("2026-03-04", 0), _at("2026-03-11", 0))
    assert (rolling.value, rolling.sample_size) == (0.8, 5)


def test_spend_per_finished_ticket_divides_spend_by_distinct_finished_tickets() -> None:
    items = [
        SeriesItem(None, "root", "session", value=9_000_000, day_utc="2026-03-02"),
        SeriesItem(None, "root", "session", value=3_000_000, day_utc="2026-03-03"),
        SeriesItem(_at("2026-03-02"), "t1", "ticket"),
        SeriesItem(_at("2026-03-03"), "t2", "ticket"),
        SeriesItem(_at("2026-03-03", 14), "t2", "ticket"),  # Finished twice: one ticket.
    ]
    value = aggregate("ratio", items)
    assert (value.value, value.sample_size) == (6.0, 2)
    assert aggregate("ratio", items[:2]).value is None


async def test_kpis_and_insights_count_people_not_agents(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "zz-active"})).json()
    path = f"/v1/projects/{created['id']}"
    stages = (await project_client.get(f"{path}/tickets")).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    ticket = (await project_client.post(f"{path}/tickets", json={"title": "By hand"})).json()
    await project_client.patch(f"{path}/tickets/{ticket['id']}", json={"stage_id": done})
    # The Lead acting for the owner isn't a person being active.
    lead = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    await project_client.post(
        f"{path}/tickets/{ticket['id']}/comments", json={"text": "Agent note"}, headers=lead
    )
    # Only the person's create and move count; the Lead's comment doesn't.
    activity = ProjectTicketStore(db_uri).human_ticket_activity(
        created["id"], user_id=None, since_epoch=None
    )
    assert [user for user, _ in activity] == ["local", "local"]
    kpis = [
        {
            "name": "Daily active people",
            "metric": "daily_active_humans",
            "why": "Engagement.",
            "target": 1,
            "direction": "at_least",
        },
        {
            "name": "Spend per ticket",
            "metric": "spend_per_finished_ticket_usd",
            "why": "Value for money.",
            "target": 10,
            "direction": "at_most",
        },
    ]
    written = await project_client.put(f"{path}/kpis", json={"kpis": kpis})
    assert written.status_code == 200, written.text
    rows = {row["metric"]: row for row in written.json()["kpis"]}
    today = datetime.now(UTC).date()
    weekday_today = today.weekday() < 5
    items = (
        await project_client.get(
            f"{path}/status/items", params={"metric": "daily_active_humans", "range": "7d"}
        )
    ).json()
    # Today may be a weekend day, which is listed as left out rather than counted.
    people = [*items["items"], *items["excluded"]]
    assert {row["id"] for row in people} == {"local"}
    assert all(row["kind"] == "person" for row in people)
    assert rows["daily_active_humans"]["value"] == items["total"]
    # One person active today, averaged over the window's weekdays.
    assert items["total"] == (round(1 / 5, 1) if weekday_today else 0)
    # No spend recorded yet: $0 over the one finished ticket.
    assert rows["spend_per_finished_ticket_usd"]["value"] == 0
    throughput = (await project_client.get(f"{path}/telemetry")).json()["throughput"]
    assert throughput["daily_active_humans"] == items["total"]
    assert throughput["spend_per_finished_ticket_usd"] == 0


def _people(body: dict) -> list[dict]:
    """Person rows counted or left out: weekend days are listed as left out."""
    return [row for row in (*body["items"], *body["excluded"]) if row["kind"] == "person"]


async def test_the_people_drill_down_follows_the_insights_privacy_rule(
    multi_user_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    owner, member = _as_user("owner@example.com"), _as_user("member@example.com")
    created = (
        await multi_user_client.post("/v1/projects", json={"name": "zz-private"}, headers=owner)
    ).json()
    path = f"/v1/projects/{created['id']}"
    await multi_user_client.post(
        f"{path}/shares", json={"user_id": "member@example.com"}, headers=owner
    )
    for headers in (owner, member):
        await multi_user_client.post(f"{path}/tickets", json={"title": "Hi"}, headers=headers)
    params = {"metric": "daily_active_humans", "range": "7d"}

    async def read(headers, **extra):
        return await multi_user_client.get(
            f"{path}/status/items", params={**params, **extra}, headers=headers
        )

    as_owner = (await read(owner)).json()
    assert {row["id"] for row in _people(as_owner)} == {
        "owner@example.com",
        "member@example.com",
    }
    assert as_owner["hidden_items"] == 0
    as_member = (await read(member)).json()
    assert [row["id"] for row in _people(as_member)] == ["member@example.com"]
    assert as_member["hidden_items"] == 1
    # Counts are the same for everyone; only names are withheld.
    assert as_member["total"] == as_owner["total"] and as_member["days"] == as_owner["days"]
    agent = {**owner, ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    as_agent = (await read(agent)).json()
    assert (_people(as_agent), as_agent["hidden_items"]) == ([], 2)
    assert as_agent["total"] == as_owner["total"]
    # One person's days: the owner may ask, a member only for themselves, an agent never.
    assert (await read(owner, member="user:member@example.com")).status_code == 200
    assert (await read(member, member="user:member@example.com")).status_code == 200
    assert (await read(member, member="user:owner@example.com")).status_code == 403
    assert (await read(agent, member="user:owner@example.com")).status_code == 403


def test_the_drill_down_lists_each_person_once_a_day_and_weekends_as_left_out() -> None:
    from omnigent.server.project_status import status_period
    from omnigent.server.project_status_items import drilldown

    series = MetricSeries(
        "weekday_mean",
        items=[
            _person("ann", "2026-03-05", 9),
            _person("ann", "2026-03-05", 15),
            _person("bob", "2026-03-06"),
            _person("bob", "2026-03-07"),
        ],
    )
    # Sunday 2026-03-08, so the 7 days are Mon 03-02 to Sun 03-08.
    result = drilldown(series, status_period("7d", _at("2026-03-08", 18)), "7d")
    assert sorted((row["day_utc"], row["id"]) for row in result["items"]) == [
        ("2026-03-05", "ann"),
        ("2026-03-06", "bob"),
    ]
    assert [(row["day_utc"], row["reason"]) for row in result["excluded"]] == [
        ("2026-03-07", "Weekend: not counted")
    ]
    assert result["total"] == 0.4  # Two people over five weekdays.
    assert {day["day_utc"]: day["items"] for day in result["days"]}["2026-03-05"] == 1
