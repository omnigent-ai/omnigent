"""Durable mixed-attachment history survives native cold resume and policy changes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.host.frames import HostHelloFrame
from omnigent.inner.native_attachments import (
    CAP_FILESYSTEM_ATTACHMENTS,
    CAP_GENERALIZED_FILESYSTEM_ATTACHMENTS,
    attachment_cache_dir,
)
from omnigent.native.native_coding_agents import native_coding_agent_for_harness
from omnigent.server.host_registry import HostRegistry, WebSocketLike
from omnigent.server.routes import sessions
from omnigent.server.server_config import filesystem_attachment_policy
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests.server.routes.test_sessions_fork import _StubAgentCache

_VIDEO = b"\x00\xff\x80durable-video-bytes"
_NOTE = b"Keep the companion note inline across a fork.\n"
_HOST_ID = "c" * 32


def _app(db_uri: str, artifacts: Path, hosts: HostRegistry | None = None) -> FastAPI:
    application = FastAPI()
    application.state.filesystem_attachment_policy = filesystem_attachment_policy()
    application.include_router(
        sessions.create_sessions_router(
            SqlAlchemyConversationStore(db_uri),
            SqlAlchemyAgentStore(db_uri),
            file_store=SqlAlchemyFileStore(db_uri),
            artifact_store=LocalArtifactStore(str(artifacts)),
            host_registry=hosts,
        ),
        prefix="/v1",
    )
    return application


def _persist_message(store: SqlAlchemyConversationStore, session_id: str, ids: list[str]) -> None:
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=uuid.uuid4().hex,
                data=MessageData(
                    role="user",
                    content=[
                        *({"type": "input_file", "file_id": file_id} for file_id in ids),
                        {"type": "input_text", "text": "Read the note and inspect the video."},
                    ],
                ),
            )
        ],
    )


async def _restore_in_fresh_process(manifest_path: Path) -> None:
    """Read only persisted server state and rebuild native artifacts in a new interpreter."""
    manifest = json.loads(manifest_path.read_text())
    root = Path(manifest["root"])
    application = _app(manifest["db_uri"], Path(manifest["artifacts"]))
    outputs: dict[str, str] = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        for session_id in manifest["session_ids"]:
            bridge = root / session_id / "bridge"
            external_id = str(uuid.uuid5(uuid.NAMESPACE_URL, session_id))
            if manifest["harness"] == "claude-native":
                from omnigent.harnesses.claude_native import main as claude

                claude._CLAUDE_PROJECTS_DIR = root / "claude-projects"
                output = await claude._ensure_local_claude_resume_transcript(
                    client,
                    session_id=session_id,
                    external_session_id=external_id,
                    workspace=root,
                    bridge_dir=bridge,
                )
                assert output is not None
            else:
                from omnigent.harnesses.codex_native import main as codex

                output = await codex._ensure_local_codex_resume_rollout(
                    client,
                    session_id=session_id,
                    external_session_id=external_id,
                    codex_home=bridge / "codex-home",
                    workspace=root,
                    model_provider="test",
                    codex_path=None,
                )
            outputs[session_id] = str(output)
    (root / "outputs.json").write_text(json.dumps(outputs))


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
async def test_mixed_history_survives_durable_native_resume(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """Fork preserves an inline companion while videos restore after allowlist removal."""
    config = tmp_path / "server.yaml"
    config.write_text('filesystem_attachment_allowed_extensions: [".mp4"]\n')
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "runner-data"))
    artifacts = tmp_path / "artifacts"
    conversations = SqlAlchemyConversationStore(db_uri)
    files = SqlAlchemyFileStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    agent = agents.create(uuid.uuid4().hex, "durable-native", "unused-bundle")
    native = native_coding_agent_for_harness(harness)
    assert native is not None
    monkeypatch.setattr(sessions, "get_agent_cache", lambda: _StubAgentCache({agent.id: harness}))
    hosts = HostRegistry()
    hosts.register(
        _HOST_ID,
        Mock(spec=WebSocketLike),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="durable-test-host",
            capabilities=[CAP_FILESYSTEM_ATTACHMENTS, CAP_GENERALIZED_FILESYSTEM_ATTACHMENTS],
        ),
        owner=None,
    )
    source = conversations.create_conversation(
        agent_id=agent.id,
        host_id=_HOST_ID,
        workspace=str(tmp_path),
        labels=native.presentation_labels,
    )
    child = conversations.create_conversation(
        parent_conversation_id=source.id,
        agent_id=agent.id,
        host_id=_HOST_ID,
        workspace=str(tmp_path),
        labels=native.presentation_labels,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(db_uri, artifacts, hosts)), base_url="http://test"
    ) as client:
        original_ids = []
        for filename, content in [("clip.mp4", _VIDEO), ("note.txt", _NOTE)]:
            response = await client.post(
                f"/v1/sessions/{source.id}/resources/files",
                files={"file": (filename, content, "text/plain")},
            )
            assert response.status_code == 201, response.text
            original_ids.append(response.json()["id"])
        video = files.get(original_ids[0], session_id=source.id)
        note = files.get(original_ids[1], session_id=source.id)
        assert video is not None and video.source_metadata == {"delivery": "filesystem"}
        assert note is not None and note.source_metadata is None
        _persist_message(conversations, source.id, original_ids)
        response = await client.post(
            f"/v1/sessions/{child.id}/resources/files:copy",
            json={"source_session_id": source.id, "file_ids": original_ids},
        )
        assert response.status_code == 200, response.text
        copied_ids = [response.json()["mapping"][fid]["new_id"] for fid in original_ids]
        assert set(copied_ids).isdisjoint(original_ids)
        _persist_message(conversations, child.id, copied_ids)

    # Restart with expanded policy without reclassifying already-inline history.
    config.write_text('filesystem_attachment_allowed_extensions: [".mp4", ".txt"]\n')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(db_uri, artifacts, hosts)), base_url="http://test"
    ) as client:
        response = await client.post(f"/v1/sessions/{source.id}/fork", json={})
        assert response.status_code == 201, response.text
        fork_id = response.json()["id"]
        fork_files = {row.filename: row for row in files.list(fork_id).data}
        assert fork_files["note.txt"].source_metadata == note.source_metadata
        assert fork_files["note.txt"].content_type == note.content_type
        assert fork_files["clip.mp4"].source_metadata == video.source_metadata
        assert fork_files["clip.mp4"].blob_key == video.blob_key
        assert fork_files["note.txt"].blob_key == note.blob_key

    session_ids = [source.id, child.id, fork_id]
    config.write_text("filesystem_attachment_allowed_extensions: []\n")
    get_or_create_engine(db_uri).dispose()
    reopened_files = SqlAlchemyFileStore(db_uri)
    reopened_history = SqlAlchemyConversationStore(db_uri)
    for session_id in session_ids:
        stored = {row.filename: row for row in reopened_files.list(session_id).data}
        assert stored["clip.mp4"].source_metadata == {"delivery": "filesystem"}
        assert stored["note.txt"].source_metadata is None
        expected_ids = {row.id for row in stored.values()}
        referenced_ids = {
            block["file_id"]
            for item in reopened_history.list_items(session_id).data
            if isinstance(item.data, MessageData)
            for block in item.data.content
            if "file_id" in block
        }
        assert referenced_ids == expected_ids
        if session_id == fork_id:
            assert expected_ids.isdisjoint(original_ids)

    root = tmp_path / "restored"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "root": str(root),
                "db_uri": db_uri,
                "artifacts": str(artifacts),
                "session_ids": session_ids,
                "harness": harness,
            }
        )
    )
    command = [
        sys.executable,
        "-c",
        "import asyncio,sys; from pathlib import Path; "
        "from tests.server.routes.test_attachment_durable_resume "
        "import _restore_in_fresh_process; "
        "asyncio.run(_restore_in_fresh_process(Path(sys.argv[1])))",
        str(manifest),
    ]
    for attempt in range(2):
        process = subprocess.run(command, capture_output=True, text=True, timeout=60)
        assert process.returncode == 0, process.stdout + process.stderr
        outputs = json.loads((root / "outputs.json").read_text())
        for session_id, output in outputs.items():
            transcript = Path(output).read_text()
            cache = attachment_cache_dir(root / session_id / "bridge")
            for filename, content in [("clip.mp4", _VIDEO), ("note.txt", _NOTE)]:
                path = cache / filename
                assert path.read_bytes() == content
                assert path.stat().st_mode & 0o777 == 0o600
                assert f"[Attached: {path}]" in transcript
            assert "could not be loaded" not in transcript
            if attempt == 0:
                shutil.rmtree(cache)
                Path(output).unlink()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(db_uri, artifacts, hosts)), base_url="http://test"
    ) as client:
        upload = await client.post(
            f"/v1/sessions/{source.id}/resources/files",
            files={"file": ("clip.mp4", _VIDEO, "video/mp4")},
        )
        copy = await client.post(
            f"/v1/sessions/{child.id}/resources/files:copy",
            json={"source_session_id": source.id, "file_ids": original_ids},
        )
        fork = await client.post(f"/v1/sessions/{source.id}/fork", json={})
    assert upload.status_code == 415, upload.text
    assert copy.status_code == 415, copy.text
    assert fork.status_code == 415, fork.text
