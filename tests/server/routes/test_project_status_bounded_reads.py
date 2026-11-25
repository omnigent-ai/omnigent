"""Bounding status reads by the earliest needed day leaves every number unchanged."""

from __future__ import annotations

import functools
import random

import httpx
import sqlalchemy as sa
from fastapi import FastAPI

from omnigent.db.utils import now_epoch
from omnigent.entities.project_kpi import KPI_METRICS, ProjectKpi
from omnigent.server import project_status_prs
from omnigent.server.project_kpis import KpiInputs, kpi_history_since, kpi_rows, load_series
from omnigent.server.project_status import StatusSources, status_period
from omnigent.server.project_status_items import drilldown, range_bounds
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    project_agents,
    project_app,
    project_client,
)

DAY = 86_400


async def _seed_long_board(client: httpx.AsyncClient, db_uri: str) -> str:
    """Tickets spread over 60 days: shipped, sent back, reopened, with PR outputs."""
    created = (await client.post("/v1/projects", json={"name": "Long"})).json()
    path = f"/v1/projects/{created['id']}"
    stages = [stage["id"] for stage in (await client.get(f"{path}/tickets")).json()["stages"]]
    rng = random.Random(5)
    shifts: dict[str, int] = {}
    for index in range(30):
        ticket = (await client.post(f"{path}/tickets", json={"title": f"T{index}"})).json()["id"]
        route = [stages[1], stages[2]]
        if index % 3 == 0:
            route += [stages[1], stages[2]]
        route.append(stages[-1])
        if index % 5 == 0:
            route.append(stages[0])  # reopened
        for stage in route:
            await client.patch(f"{path}/tickets/{ticket}", json={"stage_id": stage})
        if index % 4 == 0:
            await client.post(
                f"{path}/tickets/{ticket}/comments",
                json={
                    "text": "PR",
                    "outputs": [
                        {
                            "type": "pull_request",
                            "url": f"https://git.example.com/o/r/pull/{index}",
                        }
                    ],
                },
            )
        shifts[ticket] = rng.randint(0, 60)
    engine = sa.create_engine(db_uri)
    with engine.begin() as connection:
        for ticket, days in shifts.items():
            # Every event of a ticket moves back by its shift; creation a little further.
            connection.execute(
                sa.text(
                    "UPDATE project_ticket_events SET created_at = created_at - :delta "
                    "- CASE WHEN position = 0 THEN :lead ELSE 0 END WHERE ticket_id = :id"
                ),
                {"delta": days * DAY, "lead": 5 * 3600, "id": bytes.fromhex(ticket)},
            )
    return created["id"]


def _inputs(app: FastAPI, project, period, since: int | None) -> KpiInputs:
    tickets = app.state.project_ticket_notifications.tickets
    return KpiInputs(
        project=project,
        period=period,
        sources=StatusSources(spend=None, prs_merged=project_status_prs.build_source(app.state)),
        stage_histories=functools.cache(
            lambda: tickets.stage_histories(project.id, user_id=project.user_id, since_epoch=since)
        ),
        stages=functools.cache(
            lambda: tickets.stages(project.id, user_id=project.user_id)["stages"]
        ),
        history_since=since,
    )


async def test_bounded_reads_match_unbounded_for_every_metric_and_range(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    project_app: FastAPI,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    project_id = await _seed_long_board(project_client, db_uri)
    project = project_app.state.project_ticket_notifications.projects.get(project_id, user_id=None)
    metrics = [metric.id for metric in KPI_METRICS if metric.id != "spend_usd"]
    kpis = [
        ProjectKpi(name=metric, metric=metric, why="w", target=1, direction="at_least")
        for metric in metrics
    ]
    now = now_epoch()
    tickets = project_app.state.project_ticket_notifications.tickets
    assert len(tickets.stage_histories(project_id, user_id=None, since_epoch=now - 7 * DAY)) < len(
        tickets.stage_histories(project_id, user_id=None)
    ), "the seeded board must have tickets outside the bounded range"
    for window in ("7d", "30d", "all"):
        period = status_period(window, now)
        bounded = _inputs(project_app, project, period, kpi_history_since(period))
        unbounded = _inputs(project_app, project, period, None)
        assert kpi_rows(kpis, load_series(metrics, bounded), period) == kpi_rows(
            kpis, load_series(metrics, unbounded), period
        ), window
    for range_id in ("trend", "7d", "30d", "all"):
        period = status_period("7d" if range_id == "trend" else range_id, now)
        since = range_bounds(range_id, period)[0]
        bounded = load_series(metrics, _inputs(project_app, project, period, since))
        unbounded = load_series(metrics, _inputs(project_app, project, period, None))
        for metric in metrics:
            assert drilldown(bounded[metric], period, range_id) == drilldown(
                unbounded[metric], period, range_id
            ), (range_id, metric)
