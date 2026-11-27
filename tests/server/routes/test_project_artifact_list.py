"""Listing a project's files by time, ticket and kind; saved videos count as demos."""

from __future__ import annotations

import pytest

from tests.server.routes.test_project_file_comments import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import ALICE, _as_user
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents


@pytest.mark.asyncio
async def test_files_filter_by_ticket_kind_and_time(multi_user_app, multi_user_client):
    client = multi_user_client
    project_id = (
        await client.post("/v1/projects", json={"name": "Demos"}, headers=_as_user(ALICE))
    ).json()["id"]
    ticket_id = (
        await client.post(
            f"/v1/projects/{project_id}/tickets",
            json={"title": "Ship it"},
            headers=_as_user(ALICE),
        )
    ).json()["id"]
    base = f"/v1/projects/{project_id}/files"

    async def save(name: str, content_type: str, **form: str) -> dict:
        response = await client.post(
            base,
            files={"file": (name, b"bytes", content_type)},
            data=form,
            headers=_as_user(ALICE),
        )
        assert response.status_code == 201, response.text
        return response.json()

    notes = await save("notes.md", "text/markdown")
    video = await save("demo.mp4", "video/mp4", ticket_id=ticket_id)
    gif = await save("demo.gif", "image/gif", ticket_id=ticket_id, demo="true")
    raw = await save("raw.mp4", "video/mp4", demo="false")
    assert (video["demo"], gif["demo"], raw["demo"], notes["demo"]) == (True, True, False, False)

    async def listed(**params) -> list[str]:
        response = await client.get(base, params=params, headers=_as_user(ALICE))
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()["data"]]

    assert set(await listed()) == {notes["id"], video["id"], gif["id"], raw["id"]}
    assert set(await listed(kind="demo")) == {video["id"], gif["id"]}
    assert set(await listed(ticket_id=ticket_id)) == {video["id"], gif["id"]}
    assert await listed(since=notes["created_at"] + 10**6) == []
    assert (
        await client.get(base, params={"kind": "bogus"}, headers=_as_user(ALICE))
    ).status_code == 422
