"""Re-saving a project artifact replaces it in place, with a stable ID and URL."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.project_tickets import execute_ticket_tool
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.routes.test_project_file_comments import _agent_session
from tests.server.routes.test_project_file_comments import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import ALICE, _as_user
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents


@pytest.mark.asyncio
async def test_an_agent_replaces_its_own_artifact_in_place(
    multi_user_app, multi_user_client, db_uri, tmp_path: Path
):
    client = multi_user_client
    project_id = (
        await client.post("/v1/projects", json={"name": "Logs"}, headers=_as_user(ALICE))
    ).json()["id"]
    session_id = _agent_session(db_uri, project_id)
    runner = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=multi_user_app),
        base_url="http://test",
        headers={**_as_user(ALICE), **_bind_runner(db_uri, session_id)},
    )
    log = tmp_path / "monitor-log.md"

    async def save(**arguments) -> dict:
        return json.loads(
            await execute_ticket_tool(
                "sys_project_artifact_save",
                {"path": "monitor-log.md", "title": "Monitor log", **arguments},
                runner,
                session_id,
                tmp_path,
            )
        )["artifact"]

    log.write_text("run 1\n")
    first = await save()
    log.write_text("run 1\nrun 2\n")
    second = await save(replace_id=first["id"])
    assert (second["id"], second["url"], second["revision"]) == (first["id"], first["url"], 2)

    listed = (
        await client.get(f"/v1/projects/{project_id}/files", headers=_as_user(ALICE))
    ).json()["data"]
    assert [row["id"] for row in listed] == [first["id"]]
    content = await client.get(first["url"], headers=_as_user(ALICE))
    assert content.text == "run 1\nrun 2\n"
    assert content.headers["cache-control"] == "private, no-cache"
    etag = content.headers["etag"]
    unchanged = await client.get(first["url"], headers={**_as_user(ALICE), "If-None-Match": etag})
    assert unchanged.status_code == 304

    # An agent can't overwrite a file a person uploaded.
    uploaded = await client.post(
        f"/v1/projects/{project_id}/files",
        files={"file": ("plan.md", b"# Plan\n", "text/markdown")},
        headers=_as_user(ALICE),
    )
    refused = await client.post(
        f"/v1/projects/{project_id}/files",
        files={"file": ("plan.md", b"overwritten\n", "text/markdown")},
        data={"replace_id": uploaded.json()["id"]},
        headers={**_as_user(ALICE), ORCHESTRATION_SOURCE_HEADER: session_id},
    )
    assert refused.status_code == 403
    await runner.aclose()
