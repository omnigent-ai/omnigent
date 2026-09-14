"""Antigravity pending-input reconciliation after server file resolution."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.inner.antigravity_native_executor import _content_to_text
from omnigent.runtime import pending_inputs
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_AUTHOR = "web@example.com"


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: Any,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    process_manager = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await process_manager.start()
    set_harness_process_manager(process_manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://test"
    ) as test_client:
        yield test_client
    mock_llm.release_all()
    set_harness_process_manager(None)
    await process_manager.shutdown()


async def _create_antigravity_session(client: httpx.AsyncClient) -> str:
    agent = await create_test_agent(
        client,
        name="antigravity-pending-attachment",
        executor={"type": "omnigent", "config": {"harness": "antigravity-native"}},
        user=_AUTHOR,
    )
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        headers={"X-Forwarded-Email": _AUTHOR},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload_file(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    filename: str,
    content: bytes,
    content_type: str,
) -> str:
    response = await client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (filename, content, content_type)},
        headers={"X-Forwarded-Email": _AUTHOR},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _post_transcript_item(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    text: str,
    source_id: str,
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
                "response_id": "resp_antigravity_echo",
                "source_id": source_id,
            },
        },
        headers={"X-Forwarded-Email": _AUTHOR},
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


@pytest.mark.parametrize(
    ("attachment_type", "filename", "content_type", "payload", "caption"),
    [
        ("input_image", "photo.png", "image/png", b"image-bytes", "describe the image"),
        ("input_image", "photo.png", "image/png", b"image-no-caption", None),
        ("input_image", None, "image/png", b"image-caption-no-name", "describe the image"),
        ("input_image", None, "image/png", b"image-without-caption", None),
        ("input_file", "notes.txt", "text/plain", b"file-bytes", "summarize the file"),
        ("input_file", "notes.txt", "text/plain", b"file-no-caption", None),
        ("input_file", None, "text/plain", b"file-caption-no-name", "summarize the file"),
        ("input_file", None, "text/plain", b"file-without-caption", None),
    ],
)
async def test_antigravity_file_id_pending_input_uses_resolved_transport_content(
    auth_client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attachment_type: str,
    filename: str | None,
    content_type: str,
    payload: bytes,
    caption: str | None,
) -> None:
    """A raw uploaded file ID survives transcript persistence after native formatting."""
    from omnigent.server.routes import sessions as sessions_module

    session_id = await _create_antigravity_session(auth_client)
    uploaded_name = filename or ("photo.png" if attachment_type == "input_image" else "notes.txt")
    file_id = await _upload_file(
        auth_client,
        session_id,
        filename=uploaded_name,
        content=payload,
        content_type=content_type,
    )
    original_attachment: dict[str, Any] = {"type": attachment_type, "file_id": file_id}
    if filename is not None:
        original_attachment["filename"] = filename
    original_content = [original_attachment]
    if caption is not None:
        original_content.append({"type": "input_text", "text": caption})

    forwarded: list[dict[str, Any]] = []

    def handle_runner(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_runner), base_url="http://runner"
    ) as runner_client:

        async def get_runner(*_: Any, **__: Any) -> httpx.AsyncClient:
            return runner_client

        monkeypatch.setattr(sessions_module, "_get_runner_client", get_runner)
        responses = [
            await auth_client.post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "message", "data": {"role": "user", "content": original_content}},
                headers={"X-Forwarded-Email": _AUTHOR},
            )
            for _ in range(2)
        ]

    assert all(response.status_code == 202 for response in responses)
    pending_ids = [response.json()["pending_id"] for response in responses]
    assert len(forwarded) == 2
    resolved_content = forwarded[0]["content"]
    resolved_attachment = resolved_content[0]
    assert "file_id" not in resolved_attachment
    assert (
        resolved_attachment.get("image_url") or resolved_attachment.get("file_data")
    ).startswith("data:")

    first_text = _content_to_text(resolved_content, tmp_path / "bridge")
    persisted = await _post_transcript_item(
        auth_client,
        session_id,
        text=first_text,
        source_id=f"agy-transcript:attachment:{attachment_type}:{filename or 'unnamed'}",
    )
    duplicate = await _post_transcript_item(
        auth_client,
        session_id,
        text=first_text,
        source_id=f"agy-transcript:attachment:{attachment_type}:{filename or 'unnamed'}",
    )

    assert duplicate["item_id"] == persisted["item_id"]
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        pending_ids[1]
    ]
    second_text = _content_to_text(forwarded[1]["content"], tmp_path / "bridge")
    persisted_second = await _post_transcript_item(
        auth_client,
        session_id,
        text=second_text,
        source_id=f"agy-transcript:attachment:{attachment_type}:{filename or 'unnamed'}:second",
    )
    items = (
        await auth_client.get(
            f"/v1/sessions/{session_id}/items",
            headers={"X-Forwarded-Email": _AUTHOR},
        )
    ).json()["data"]
    items = [item for item in items if item["type"] == "message"]
    assert len(items) == 2
    assert items[0]["id"] == persisted["item_id"]
    assert items[0]["type"] == "message"
    assert items[0]["role"] == "user"
    assert items[0]["created_by"] == _AUTHOR
    assert items[0]["content"] == [
        original_attachment,
        {"type": "input_text", "text": first_text},
    ]
    assert items[1]["id"] == persisted_second["item_id"]
    assert items[1]["created_by"] == _AUTHOR
    assert items[1]["content"] == [
        original_attachment,
        {"type": "input_text", "text": second_text},
    ]
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == []
    assert all(pending_id.startswith("pending_") for pending_id in pending_ids)
