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
    KpiInputs,
    MetricValue,
    compute_metrics,
    hours_to_final_stage,
    kpi_status,
    nearest_rank,
    rework_count,
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
    # A ticket sent back twice counts once.
    assert rework_count(histories, ORDER, period) == 2
    assert rework_count(histories, ORDER, status_period("all", NOW)) == 3


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
    assert sorted(hours_to_final_stage(histories, "merged", period)) == [2, 8]
    assert hours_to_final_stage(histories, None, period) == []
    assert nearest_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    assert nearest_rank([4], 0.9) == 4


def test_compute_reads_only_what_the_kpis_need() -> None:
    project = SimpleNamespace(id="p")
    period = status_period("7d", NOW)
    created = NOW - 10 * HOUR

    def must_not_run():
        raise AssertionError("read data no KPI needs")

    spend = SimpleNamespace(spend=lambda project, period: MemberTotals(total=12345))
    inputs = KpiInputs(
        project=project,
        period=period,
        sources=StatusSources(spend=spend, prs_merged=None),
        completed_moves=lambda: [{"ticket_id": "a"}, {"ticket_id": "a"}, {"ticket_id": "b"}],
        stage_histories=lambda: {
            "a": [_entry("open", created, created=True), _entry("merged", created + 3 * HOUR)]
        },
        stages=lambda: [{"id": stage, "is_complete": stage == "merged"} for stage in ORDER],
    )
    values = compute_metrics(
        ["spend_usd", "merged_prs", "tickets_shipped", "time_to_merged_p90_hours", "none"],
        inputs,
    )
    assert values["spend_usd"] == MetricValue(123.45)
    # No PRs source on this server: unavailable, not zero.
    assert not values["merged_prs"].available
    assert values["tickets_shipped"] == MetricValue(2)
    assert values["time_to_merged_p90_hours"] == MetricValue(3.0, sample_size=1)
    assert "none" not in values
    lazy = KpiInputs(
        project=project,
        period=period,
        sources=StatusSources(spend=spend, prs_merged=None),
        completed_moves=must_not_run,
        stage_histories=must_not_run,
        stages=must_not_run,
    )
    assert set(compute_metrics(["spend_usd"], lazy)) == {"spend_usd"}


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
