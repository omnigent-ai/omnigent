"""Stage moves read from the projection table give exactly the old event-replay results."""

from __future__ import annotations

import functools

import httpx
from fastapi import FastAPI

from omnigent.db.utils import now_epoch
from omnigent.entities.project_kpi import KPI_METRICS, ProjectKpi
from omnigent.server.project_kpis import KpiInputs, kpi_history_since, kpi_rows, load_series
from omnigent.server.project_status import StatusSources, status_period
from omnigent.server.project_status_items import drilldown, range_bounds
from omnigent.server.routes.project_status import move_member_resolver, tickets_done_by_member
from omnigent.stores.project_ticket_store import ProjectTicketStore
from tests.server.routes.test_project_status_bounded_reads import _seed_long_board
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    project_agents,
    project_app,
    project_client,
)

METRICS = [metric.id for metric in KPI_METRICS if metric.id not in ("spend_usd", "merged_prs")]


def _strip(histories):
    return {
        ticket: [{k: v for k, v in entry.items() if k != "member_key"} for entry in history]
        for ticket, history in histories.items()
    }


def _outputs(store: ProjectTicketStore, project, conversations) -> dict:
    """Every number the Summary, KPI tiles and drill-down derive from stage moves."""
    now = now_epoch()
    member_of = move_member_resolver(project, conversations)
    kpis = [ProjectKpi(name=m, metric=m, why="w", target=1, direction="at_least") for m in METRICS]
    result: dict = {}
    for window in ("7d", "30d", "all"):
        period = status_period(window, now)
        moves = store.completed_moves(project.id, user_id=None, since_epoch=period.since_epoch)
        done = tickets_done_by_member(project, moves, conversations)
        result[f"done-{window}"] = (done.total, dict(done.by_member))
        since = kpi_history_since(period)
        inputs = KpiInputs(
            project=project,
            period=period,
            sources=StatusSources(None, None),
            stage_histories=functools.cache(
                lambda since=since: store.stage_histories(
                    project.id, user_id=None, since_epoch=since
                )
            ),
            stages=lambda: store.stages(project.id, user_id=None)["stages"],
            history_since=since,
        )
        result[f"kpis-{window}"] = kpi_rows(kpis, load_series(METRICS, inputs), period)
    for range_id in ("trend", "7d", "30d", "all"):
        period = status_period("7d" if range_id == "trend" else range_id, now)
        since = range_bounds(range_id, period)[0]
        inputs = KpiInputs(
            project=project,
            period=period,
            sources=StatusSources(None, None),
            stage_histories=lambda since=since: store.stage_histories(
                project.id, user_id=None, since_epoch=since
            ),
            stages=lambda: store.stages(project.id, user_id=None)["stages"],
            history_since=since,
        )
        series = load_series(METRICS, inputs, member_of)
        for metric in METRICS:
            result[f"drill-{range_id}-{metric}"] = drilldown(series[metric], period, range_id)
    result["histories"] = _strip(store.stage_histories(project.id, user_id=None))
    return result


async def test_table_reads_match_event_replay_through_moves_and_stage_changes(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
    project_app: FastAPI,  # noqa: F811 - imported fixture
    db_uri: str,
) -> None:
    project_id = await _seed_long_board(project_client, db_uri)
    state = project_app.state.project_ticket_notifications
    tables: ProjectTicketStore = state.tickets
    replay = ProjectTicketStore(state.projects.storage_location)  # no resolver: event replay
    conversations = state.inputs.conversations
    project = state.projects.get(project_id, user_id=None)
    # The first read builds the table from the (back-dated) events.
    assert _outputs(tables, project, conversations) == _outputs(replay, project, conversations)
    assert tables._ensure_moves_built(project_id, None) is True
    assert replay._ensure_moves_built(project_id, None) is False

    path = f"/v1/projects/{project_id}"
    stages = [
        stage["id"] for stage in (await project_client.get(f"{path}/tickets")).json()["stages"]
    ]
    ticket = (await project_client.post(f"{path}/tickets", json={"title": "Live"})).json()["id"]
    # Shipped, reopened (rework), shipped again: each write keeps the table current.
    for stage in (stages[1], stages[-1], stages[0], stages[2], stages[-1]):
        await project_client.patch(f"{path}/tickets/{ticket}", json={"stage_id": stage})
    # A stage-definition change that removes a stage moves its tickets with events too.
    current = (await project_client.get(f"{path}/tickets/stages")).json()["stages"]
    kept = [stage for stage in current if stage["id"] != stages[2]]
    changed = await project_client.put(
        f"{path}/tickets/stages",
        json={
            "stages": [
                {key: stage[key] for key in ("id", "name", "instructions", "color", "icon")}
                for stage in kept
            ],
            "moves": {stages[2]: stages[1]},
        },
    )
    assert changed.status_code == 200, changed.text
    project = state.projects.get(project_id, user_id=None)
    assert _outputs(tables, project, conversations) == _outputs(replay, project, conversations)
