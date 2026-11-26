"""The rolling 24h window, finish/land times on ticket rows, and the daily telemetry series."""

from __future__ import annotations

import httpx

from omnigent.db.utils import now_epoch
from omnigent.server.project_kpis import ticket_times
from omnigent.server.project_status import status_period
from omnigent.server.project_status_cache import period_key
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    project_agents,
    project_app,
    project_client,
)

NOW = 1_795_700_000  # 2026-11-26T08:53:20Z
STAGES = [
    {"id": stage, "is_complete": stage == "merged"}
    for stage in ("open", "code_review", "implemented", "merged")
]


def _entry(stage_id: str, at: int, *, created: bool = False) -> dict:
    return {
        "stage_id": stage_id,
        "is_complete": stage_id == "merged",
        "at": at,
        "created": created,
    }


def test_the_24h_window_starts_exactly_a_day_ago_and_isnt_cached_by_day() -> None:
    period = status_period("24h", NOW)
    assert (period.since_epoch, period.since_day_utc, period.today_utc) == (
        NOW - 86_400,
        "2026-11-25",
        "2026-11-26",
    )
    assert period.contains(NOW - 86_400) and not period.contains(NOW - 86_401)
    assert period_key(period) != period_key(status_period("24h", NOW + 60))
    assert period_key(status_period("7d", NOW)) == period_key(status_period("7d", NOW + 60))


def test_ticket_times_follow_the_latest_moves_while_the_ticket_is_still_there() -> None:
    histories = {
        "merged": [
            _entry("open", 1, created=True),
            _entry("implemented", 10),
            _entry("merged", 20),
        ],
        # Reopened after merging: neither time applies now.
        "reopened": [
            _entry("open", 1, created=True),
            _entry("implemented", 10),
            _entry("merged", 20),
            _entry("open", 30),
        ],
        # Landed twice: the later landing counts.
        "relanded": [
            _entry("open", 1, created=True),
            _entry("implemented", 10),
            _entry("code_review", 15),
            _entry("implemented", 25),
        ],
        "created-done": [_entry("merged", 5, created=True)],
    }
    assert ticket_times(histories, STAGES, {}) == {
        "merged": {"completed_at": 20, "landed_at": 10},
        "reopened": {"completed_at": None, "landed_at": None},
        "relanded": {"completed_at": None, "landed_at": 25},
        "created-done": {"completed_at": None, "landed_at": None},
    }


async def test_list_rows_carry_finish_times_and_filter_what_finished_since(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "zz-windows"})).json()
    path = f"/v1/projects/{created['id']}"
    stages = (await project_client.get(f"{path}/tickets")).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    before = now_epoch() - 1
    ids = []
    for title in ("Shipped", "Open"):
        ticket = (await project_client.post(f"{path}/tickets", json={"title": title})).json()
        ids.append(ticket["id"])
    await project_client.patch(f"{path}/tickets/{ids[0]}", json={"stage_id": done})

    rows = {
        row["title"]: row for row in (await project_client.get(f"{path}/tickets")).json()["data"]
    }
    assert rows["Shipped"]["completed_at"] >= before
    assert rows["Open"]["completed_at"] is None and rows["Open"]["landed_at"] is None
    recent = (
        await project_client.get(f"{path}/tickets", params={"completed_since": before})
    ).json()
    assert ([row["title"] for row in recent["data"]], recent["total"]) == (["Shipped"], 1)
    later = (
        await project_client.get(f"{path}/tickets", params={"completed_since": now_epoch() + 60})
    ).json()
    assert later["data"] == []

    trends = (await project_client.get(f"{path}/status/trends", params={"window": "24h"})).json()
    telemetry = (
        await project_client.get(f"{path}/telemetry", params={"window": "24h", "series": "daily"})
    ).json()
    assert telemetry["window"] == "24h" and telemetry["throughput"]["tickets_shipped"] == 1
    assert telemetry["daily"] == {"bucket": trends["bucket"], "days": trends["days"]}
    assert sum(day["tickets_finished"] for day in telemetry["daily"]["days"]) == 1
    plain = (await project_client.get(f"{path}/telemetry")).json()
    assert plain["daily"] is None
    kpis = await project_client.get(f"{path}/kpis", params={"window": "24h"})
    assert kpis.status_code == 200, kpis.text
