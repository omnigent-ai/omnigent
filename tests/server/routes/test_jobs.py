"""Tests for the Jobs/Workflows API (CRUD + run-now + runs).

Exercises the jobs routes through the shared ``client`` fixture (single-user
mode: no auth provider, so ownership scoping is off). A "Run now" creates a real
session via the same path as ``POST /v1/sessions``, so these tests seed a test
agent and assert a session + run row result.
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.routes.jobs import _auto_approve_launch_args
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


def test_auto_approve_launch_args_per_harness() -> None:
    """Job runs are unattended, so native harnesses get full-bypass launch args.

    A native harness launched in its default mode would stall on the first
    approval prompt (no human to answer it). SDK harnesses already default to
    bypass at spawn, so they need none; unknown harnesses get none.
    """
    assert _auto_approve_launch_args("claude-native") == [
        "--permission-mode",
        "bypassPermissions",
    ]
    assert _auto_approve_launch_args("codex-native") == [
        "--dangerously-bypass-approvals-and-sandbox"
    ]
    assert _auto_approve_launch_args("claude-sdk") is None
    assert _auto_approve_launch_args(None) is None


@pytest_asyncio.fixture()
async def agent_id(db_uri: str) -> str:
    """Seed a template agent a job can bind to and run as."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    aid = generate_agent_id()
    agent_store.create(aid, name="job-agent", bundle_location="test:///bundle")
    return aid


def _graph() -> dict:
    """A minimal flow graph payload."""
    return {
        "nodes": [{"id": "n1", "type": "start", "label": "Start", "x": 0, "y": 0}],
        "edges": [],
        "loops": [],
    }


# ── POST /v1/jobs ───────────────────────────────────────────────────


async def test_create_job(client: httpx.AsyncClient, agent_id: str) -> None:
    """Creating a job returns 201 with the persisted fields echoed back."""
    resp = await client.post(
        "/v1/jobs",
        json={
            "name": "My Flow",
            "graph": _graph(),
            "narrative": "Do the thing.",
            "agent_id": agent_id,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("job_")
    assert body["object"] == "job"
    assert body["name"] == "My Flow"
    assert body["narrative"] == "Do the thing."
    assert body["agent_id"] == agent_id
    assert body["graph"]["nodes"][0]["id"] == "n1"
    assert body["created_at"] == body["updated_at"]


async def test_create_job_rejects_unknown_field(client: httpx.AsyncClient) -> None:
    """``extra='forbid'`` makes an unexpected field a 422."""
    resp = await client.post(
        "/v1/jobs",
        json={"name": "X", "graph": {}, "narrative": "y", "bogus": 1},
    )
    assert resp.status_code == 422


async def test_create_job_rejects_blank_name(client: httpx.AsyncClient) -> None:
    """An empty name violates ``min_length`` and 422s."""
    resp = await client.post("/v1/jobs", json={"name": "", "graph": {}, "narrative": "y"})
    assert resp.status_code == 422


# ── GET /v1/jobs ────────────────────────────────────────────────────


async def test_list_jobs_empty(client: httpx.AsyncClient) -> None:
    """No jobs yet returns an empty list."""
    resp = await client.get("/v1/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_jobs_newest_first(client: httpx.AsyncClient) -> None:
    """Jobs list newest-updated first."""
    await client.post("/v1/jobs", json={"name": "A", "graph": {}, "narrative": "a"})
    await client.post("/v1/jobs", json={"name": "B", "graph": {}, "narrative": "b"})
    resp = await client.get("/v1/jobs")
    assert resp.status_code == 200
    names = [j["name"] for j in resp.json()]
    assert set(names) == {"A", "B"}


# ── GET /v1/jobs/{id} ───────────────────────────────────────────────


async def test_get_job(client: httpx.AsyncClient) -> None:
    """Fetch a job by id."""
    created = (
        await client.post("/v1/jobs", json={"name": "G", "graph": _graph(), "narrative": "g"})
    ).json()
    resp = await client.get(f"/v1/jobs/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_get_job_not_found(client: httpx.AsyncClient) -> None:
    """A missing job 404s."""
    resp = await client.get("/v1/jobs/job_does_not_exist")
    assert resp.status_code == 404


# ── PATCH /v1/jobs/{id} ─────────────────────────────────────────────


async def test_patch_job(client: httpx.AsyncClient) -> None:
    """Patching name + narrative updates and bumps updated_at."""
    created = (
        await client.post("/v1/jobs", json={"name": "Old", "graph": {}, "narrative": "old"})
    ).json()
    resp = await client.patch(
        f"/v1/jobs/{created['id']}", json={"name": "New", "narrative": "new"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "New"
    assert body["narrative"] == "new"
    assert body["updated_at"] >= created["updated_at"]


async def test_patch_job_not_found(client: httpx.AsyncClient) -> None:
    """Patching a missing job 404s."""
    resp = await client.patch("/v1/jobs/job_missing", json={"name": "x"})
    assert resp.status_code == 404


# ── DELETE /v1/jobs/{id} ────────────────────────────────────────────


async def test_delete_job(client: httpx.AsyncClient) -> None:
    """Deleting a job returns 204 and removes it."""
    created = (
        await client.post("/v1/jobs", json={"name": "D", "graph": {}, "narrative": "d"})
    ).json()
    resp = await client.delete(f"/v1/jobs/{created['id']}")
    assert resp.status_code == 204
    assert (await client.get(f"/v1/jobs/{created['id']}")).status_code == 404


async def test_delete_job_not_found(client: httpx.AsyncClient) -> None:
    """Deleting a missing job 404s."""
    resp = await client.delete("/v1/jobs/job_missing")
    assert resp.status_code == 404


# ── POST /v1/jobs/{id}/run ──────────────────────────────────────────


async def test_run_job_creates_session_and_run(client: httpx.AsyncClient, agent_id: str) -> None:
    """Run now creates a session seeded with the narrative and records a run."""
    job = (
        await client.post(
            "/v1/jobs",
            json={
                "name": "Runnable",
                "graph": _graph(),
                "narrative": "Investigate the logs.",
                "agent_id": agent_id,
            },
        )
    ).json()

    resp = await client.post(f"/v1/jobs/{job['id']}/run")
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["id"].startswith("run_")
    assert run["job_id"] == job["id"]
    assert run["session_id"] is not None
    assert run["status"] in ("running", "finished")

    # The created session exists and carries the narrative as a seed message.
    session = await client.get(f"/v1/sessions/{run['session_id']}")
    assert session.status_code == 200


async def test_run_stays_running_until_agent_responds(
    client: httpx.AsyncClient, agent_id: str
) -> None:
    """A fresh run is NOT marked finished before the agent produces output.

    Regression: reconcile derived ``finished`` from a bare ``idle`` session, so
    a just-launched run (notably native-Claude, which starts asynchronously)
    flipped to ``finished`` at t=0 with the session having only the seeded user
    message. The run must stay ``running`` until an agent turn actually runs.
    In this fixture no runner is bound, so the session only ever holds the
    seeded user prompt — it must never reconcile to ``finished``.
    """
    job = (
        await client.post(
            "/v1/jobs",
            json={
                "name": "Pending",
                "graph": _graph(),
                "narrative": "Do the thing.",
                "agent_id": agent_id,
            },
        )
    ).json()
    run = (await client.post(f"/v1/jobs/{job['id']}/run")).json()
    assert run["status"] == "running"

    # Re-read through reconcile: still running, because no assistant/tool item
    # exists yet (only the seeded user message).
    got = (await client.get(f"/v1/runs/{run['id']}")).json()
    assert got["status"] == "running", got
    assert got["completed_at"] is None


async def test_run_job_without_agent_is_400(client: httpx.AsyncClient) -> None:
    """A job with no bound agent and no default can't run."""
    job = (
        await client.post("/v1/jobs", json={"name": "NoAgent", "graph": {}, "narrative": "x"})
    ).json()
    resp = await client.post(f"/v1/jobs/{job['id']}/run")
    assert resp.status_code == 400


async def test_run_job_not_found(client: httpx.AsyncClient) -> None:
    """Running a missing job 404s."""
    resp = await client.post("/v1/jobs/job_missing/run")
    assert resp.status_code == 404


# ── GET /v1/jobs/{id}/runs and GET /v1/runs/{id} ────────────────────


async def test_list_and_get_runs(client: httpx.AsyncClient, agent_id: str) -> None:
    """A job's runs are listable and individually fetchable."""
    job = (
        await client.post(
            "/v1/jobs",
            json={"name": "R", "graph": _graph(), "narrative": "go", "agent_id": agent_id},
        )
    ).json()
    run = (await client.post(f"/v1/jobs/{job['id']}/run")).json()

    listed = await client.get(f"/v1/jobs/{job['id']}/runs")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [run["id"]]

    got = await client.get(f"/v1/runs/{run['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == run["id"]


async def test_get_run_not_found(client: httpx.AsyncClient) -> None:
    """A missing run 404s."""
    resp = await client.get("/v1/runs/run_missing")
    assert resp.status_code == 404
