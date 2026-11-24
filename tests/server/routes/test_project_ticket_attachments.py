"""Ticket attachment uploads: images and videos, by type and size."""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.routes import project_ticket_attachments as attachments
from tests.server.routes.test_projects_crud import project_agents as project_agents
from tests.server.routes.test_projects_crud import project_app as project_app
from tests.server.routes.test_projects_crud import project_client as project_client

pytestmark = pytest.mark.asyncio


async def _project(client: httpx.AsyncClient) -> str:
    created = await client.post("/v1/projects", json={"name": "Attachments"})
    assert created.status_code == 200, created.text
    return created.json()["id"]


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("shot.png", "image/png"),
        ("clip.mp4", "video/mp4"),
        ("clip.webm", "video/webm"),
        ("clip.mov", "video/quicktime"),
    ],
)
async def test_images_and_videos_upload_and_serve_inline(
    project_client: httpx.AsyncClient, filename: str, content_type: str
) -> None:
    project = await _project(project_client)
    uploaded = await project_client.post(
        f"/v1/projects/{project}/ticket-attachments",
        files={"file": (filename, b"media-bytes", content_type)},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["content_type"] == content_type
    served = await project_client.get(body["url"])
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith(content_type)
    assert served.content == b"media-bytes"


async def test_other_types_are_refused(project_client: httpx.AsyncClient) -> None:
    project = await _project(project_client)
    refused = await project_client.post(
        f"/v1/projects/{project}/ticket-attachments",
        files={"file": ("notes.txt", b"text", "text/plain")},
    )
    assert refused.status_code == 400, refused.text
    assert "MP4, WebM or MOV" in refused.text


async def test_videos_get_the_larger_limit(
    project_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(attachments, "MAX_TICKET_IMAGE_BYTES", 4)
    monkeypatch.setattr(attachments, "MAX_TICKET_VIDEO_BYTES", 8)
    project = await _project(project_client)
    path = f"/v1/projects/{project}/ticket-attachments"
    image = await project_client.post(path, files={"file": ("a.png", b"123456", "image/png")})
    assert image.status_code == 400 and "Images must be" in image.text
    video = await project_client.post(path, files={"file": ("a.mp4", b"123456", "video/mp4")})
    assert video.status_code == 201, video.text
    too_big = await project_client.post(path, files={"file": ("b.mp4", b"123456789", "video/mp4")})
    assert too_big.status_code == 400 and "Videos must be" in too_big.text
