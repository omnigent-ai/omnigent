"""Project status summary: window semantics, member attribution and the endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.server import project_status
from omnigent.server.project_status import (
    LEAD_MEMBER_KEY,
    WORKERS_MEMBER_KEY,
    MemberTotals,
    build_summary,
    member_key_for_session,
    status_period,
    summary_members,
)
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    _as_user,
    multi_user_app,
    multi_user_client,
    project_agents,
    project_app,
    project_client,
)


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


def test_windows_are_utc_days_ending_today() -> None:
    """A window of N days is today (UTC) plus the N-1 days before it, from midnight UTC."""
    now = _epoch("2026-03-10T00:30:00")
    week = status_period("7d", now)
    assert (week.today_utc, week.since_day_utc) == ("2026-03-10", "2026-03-04")
    assert week.since_epoch == _epoch("2026-03-04T00:00:00")
    assert week.contains(_epoch("2026-03-04T00:00:00"))
    assert not week.contains(_epoch("2026-03-03T23:59:59"))
    assert week.contains_day("2026-03-10") and not week.contains_day("2026-03-03")
    assert status_period("30d", now).since_day_utc == "2026-02-09"
    everything = status_period("all", now)
    assert everything.since_day_utc is None and everything.contains(0)
    with pytest.raises(ValueError):
        status_period("1d", now)  # type: ignore[arg-type]


def test_each_session_counts_for_one_member() -> None:
    project = SimpleNamespace(
        main_session_id="lead", retired_main_session_ids=["old-lead"], user_id="owner@example.com"
    )

    def session(session_id: str, labels: dict | None = None, family: str | None = None):
        return SimpleNamespace(id=session_id, labels=labels or {}, family_id=family)

    assert member_key_for_session(project, session("lead")) == LEAD_MEMBER_KEY
    assert member_key_for_session(project, session("old-lead")) == LEAD_MEMBER_KEY
    reviewer = session("r", {PROJECT_IDENTITY_LABEL_KEY: "rev"})
    assert member_key_for_session(project, reviewer) == "identity:rev"
    assert member_key_for_session(project, session("w", family="lead")) == WORKERS_MEMBER_KEY
    assert (
        member_key_for_session(project, session("own", family="own")) == "user:owner@example.com"
    )
    mine = session("m")
    assert member_key_for_session(project, mine, owner_user_id="bob@example.com") == (
        "user:bob@example.com"
    )


def test_unavailable_sources_report_null_not_zero() -> None:
    project = SimpleNamespace(
        id="p", user_id="owner@example.com", lead_removed=False, main_session_id="lead"
    )
    roster = summary_members(project, [], ["bob@example.com"], {"user:gone@example.com"})
    assert [row["key"] for row in roster] == [
        "user:owner@example.com",
        "user:bob@example.com",
        LEAD_MEMBER_KEY,
        WORKERS_MEMBER_KEY,
        "user:gone@example.com",
    ]
    period = status_period("7d", _epoch("2026-03-10T12:00:00"))
    summary = build_summary(
        project,
        period,
        roster=roster,
        tickets_done=MemberTotals(2, {LEAD_MEMBER_KEY: 2}),
        spend=MemberTotals(1001, {LEAD_MEMBER_KEY: 1001}),
        prs_merged=project_status.UNAVAILABLE,
    )
    assert summary["totals"] == {
        "tickets_done": 2,
        "prs_merged": None,
        "spend_cents": 1001,
        "cost_per_merged_pr_cents": None,
        "members": 5,
    }
    assert summary["sources"] == {"spend": "available", "prs_merged": "unavailable"}
    with_prs = build_summary(
        project,
        period,
        roster=roster,
        tickets_done=MemberTotals(),
        spend=MemberTotals(1001, {LEAD_MEMBER_KEY: 1001}),
        prs_merged=MemberTotals(2, {LEAD_MEMBER_KEY: 2}),
    )
    lead = next(row for row in with_prs["members"] if row["key"] == LEAD_MEMBER_KEY)
    assert (lead["prs_merged"], lead["cost_per_merged_pr_cents"]) == (2, 501)
    assert with_prs["totals"]["cost_per_merged_pr_cents"] == 501


async def test_summary_counts_completed_moves_and_reports_missing_sources(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    project = f"/v1/projects/{created['id']}"
    stages = (await project_client.get(f"{project}/tickets")).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    ticket = (await project_client.post(f"{project}/tickets", json={"title": "T"})).json()
    await project_client.patch(f"{project}/tickets/{ticket['id']}", json={"stage_id": done})
    # Reopened and completed again: still one ticket done.
    first = stages[0]["id"]
    await project_client.patch(f"{project}/tickets/{ticket['id']}", json={"stage_id": first})
    await project_client.patch(f"{project}/tickets/{ticket['id']}", json={"stage_id": done})
    response = await project_client.get(f"{project}/status/summary", params={"window": "30d"})
    assert response.status_code == 200
    body = response.json()
    assert (body["window"], body["time_zone"], body["totals"]["tickets_done"]) == (
        "30d",
        "UTC",
        1,
    )
    # The spend source is installed; this project has no recorded spend yet.
    assert body["sources"] == {"spend": "available", "prs_merged": "unavailable"}
    assert body["totals"]["spend_cents"] == 0
    owner = next(row for row in body["members"] if row["kind"] == "human")
    assert owner["tickets_done"] == 1
    assert (await project_client.get(f"{project}/status/summary?window=1d")).status_code == 422


async def test_any_project_member_reads_the_summary(
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
    read = await multi_user_client.get(f"{path}/status/summary", headers=member)
    assert read.status_code == 200
    keys = [row["key"] for row in read.json()["members"]]
    assert keys[:2] == ["user:owner@example.com", "user:member@example.com"]
    denied = await multi_user_client.get(f"{path}/status/summary", headers=stranger)
    assert denied.status_code == 404


def test_sources_are_found_by_module_and_missing_ones_stay_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source ticket adds its module; the summary picks it up without shared wiring."""
    import sys
    import types

    spend = object()
    module = types.ModuleType(project_status.SPEND_SOURCE_MODULE)
    module.build_source = lambda app_state: (app_state, spend)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, project_status.SPEND_SOURCE_MODULE, module)
    loaded = project_status.load_status_sources("state")
    assert loaded.spend == ("state", spend)
    assert loaded.prs_merged is None
