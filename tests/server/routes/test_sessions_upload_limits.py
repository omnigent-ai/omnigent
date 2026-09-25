"""Attachment upload type/size enforcement on POST /v1/sessions/{id}/resources/files."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT
from omnigent.host.frames import HOST_CAPABILITIES, HostHelloFrame
from omnigent.inner.native_attachments import MAX_FILESYSTEM_ATTACHMENT_UPLOAD_BYTES
from omnigent.runtime.content_resolver import (
    MAX_TEXT_UPLOAD_BYTES,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


@pytest.fixture
def upload_client(db_uri: str, tmp_path) -> Iterator[tuple[TestClient, str]]:
    """A sessions route client with file + artifact stores and one session."""
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store.create(
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        name="test-agent",
        bundle_location="087b7cb7ac30abf4debfaa578d052ec6/bundle",
    )
    conv = conversation_store.create_conversation(
        title="upload session", agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    # A Claude Code session, so filesystem types are accepted.
    conversation_store.set_labels(conv.id, CLAUDE_NATIVE_CODING_AGENT.presentation_labels)
    conversation_store.set_host_id(
        conv.id, "d75381f2c94b4e49a3c684946d4ddbc4", workspace=str(tmp_path)
    )
    host_registry = HostRegistry()
    host_registry.register(
        "d75381f2c94b4e49a3c684946d4ddbc4",
        Mock(),
        HostHelloFrame(
            version="0.15.0",
            frame_protocol_version=1,
            name="upload",
            capabilities=HOST_CAPABILITIES,
        ),
        owner=None,
    )

    app = FastAPI()
    app.state.host_registry = host_registry

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            file_store=file_store,
            artifact_store=artifact_store,
            host_registry=host_registry,
        ),
        prefix="/v1",
    )

    with TestClient(app) as client:
        yield client, conv.id


def _upload(
    client: TestClient,
    session_id: str,
    filename: str,
    data: bytes = b"data",
    content_type: str = "application/octet-stream",
) -> httpx.Response:
    """Post a multipart file through the actual upload route."""
    return client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (filename, data, content_type)},
    )


@pytest.mark.parametrize(
    "filename,content_type",
    [
        ("notes.txt", "text/plain"),
        ("archive.zip", "application/zip"),
        ("deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("app.db", "application/octet-stream"),
        ("report.docx", "application/zip"),
        ("data.csv", "application/vnd.ms-excel"),
    ],
)
def test_upload_supported_types(
    upload_client: tuple[TestClient, str], filename: str, content_type: str
) -> None:
    """Supported extensions remain usable even with common browser MIME mismatches."""
    client, session_id = upload_client
    response = _upload(client, session_id, filename, content_type=content_type)
    assert response.status_code == 201, response.text
    assert response.json()["name"] == filename


def test_upload_rejects_unsupported_type(upload_client: tuple[TestClient, str]) -> None:
    """Unsupported formats are rejected with 415."""
    client, session_id = upload_client
    resp = _upload(client, session_id, "clip.mp4", b"\x00\x00\x00 fake mp4 bytes", "video/mp4")
    assert resp.status_code == 415, resp.text
    assert "Unsupported attachment type" in resp.text


def test_upload_rejects_filesystem_types_for_an_unsupported_harness(
    upload_client: tuple[TestClient, str], db_uri: str
) -> None:
    """An SDK session refuses formats requiring native filesystem tools."""
    client, _ = upload_client
    sdk_session = SqlAlchemyConversationStore(db_uri).create_conversation(
        title="sdk session", agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    resp = client.post(
        f"/v1/sessions/{sdk_session.id}/resources/files",
        files={"file": ("archive.zip", b"PK\x03\x04 fake zip", "application/zip")},
    )
    assert resp.status_code == 415, resp.text
    assert "Claude Code or Codex" in resp.text


@pytest.mark.parametrize(
    "block",
    [
        {
            "type": "input_file",
            "filename": "payload.zip",
            "file_data": "data:application/zip;base64,UEs=",
        },
        # A file_id alongside inline bytes would skip re-resolution, so it is refused too.
        {
            "type": "input_file",
            "file_id": "file_abc",
            "filename": "payload.zip",
            "file_data": "data:application/zip;base64,UEs=",
        },
    ],
)
def test_message_cannot_inline_a_filesystem_attachment(
    upload_client: tuple[TestClient, str], block: dict[str, str]
) -> None:
    """Inline bytes would reach the harness without the upload route's checks."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "message", "data": {"role": "user", "content": [block]}},
    )
    assert resp.status_code == 400, resp.text
    assert "payload.zip" in resp.text


@pytest.mark.parametrize("transition", ["switch-agent", "fork"])
@pytest.mark.parametrize(
    "filename,target_harness,event_type,block_type,status",
    [
        ("archive.zip", "cursor-native", "message", "input_file", 415),
        ("archive.zip", "openai-agents", "message", "input_file", 415),
        ("archive.zip", "openai-agents", "message", "input_image", 415),
        ("archive.zip", "cursor-native", "slash_command", "input_file", 415),
        ("archive.zip", "claude-native", "message", "input_file", 202),
        ("archive.zip", "codex-native", "message", "input_file", 202),
        ("notes.txt", "openai-agents", "message", "input_file", 202),
    ],
)
def test_send_rechecks_unsent_upload_after_harness_transition(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    filename: str,
    target_harness: str,
    event_type: str,
    block_type: str,
    status: int,
) -> None:
    """Stored filenames govern admission before policy, persistence, or runner dispatch."""
    from omnigent.server.routes import sessions
    from omnigent.server.routes.sessions import routes_events

    client, source_id = upload_client
    uploaded = _upload(client, source_id, filename)
    assert uploaded.status_code == 201, uploaded.text
    target = SqlAlchemyAgentStore(db_uri).create("c" * 32, "target", "target/bundle")

    def load(_agent_id: str, bundle_location: str, **_kwargs: object) -> SimpleNamespace:
        harness = target_harness if bundle_location == target.bundle_location else "claude-native"
        return SimpleNamespace(
            spec=SimpleNamespace(executor=SimpleNamespace(harness_kind=harness))
        )

    monkeypatch.setattr(sessions, "get_agent_cache", lambda: SimpleNamespace(load=load))
    changed = client.post(f"/v1/sessions/{source_id}/{transition}", json={"agent_id": target.id})
    assert changed.status_code == (201 if transition == "fork" else 200), changed.text
    session_id = changed.json()["id"]
    files = SqlAlchemyFileStore(db_uri).list(session_id).data
    assert len(files) == 1
    assert files[0].filename == filename
    conversations = SqlAlchemyConversationStore(db_uri)
    before = conversations.list_items(session_id).data

    # Stop admitted inputs at policy so the test never needs a live runner.
    policy = AsyncMock(return_value={"verdict": "deny", "reason": "test policy"})
    dispatch = AsyncMock()
    monkeypatch.setattr(routes_events, "_evaluate_input_policy", policy)
    monkeypatch.setattr(routes_events, "_persist_policy_deny_sentinel", AsyncMock())
    monkeypatch.setattr(routes_events, "_dispatch_session_event_to_runner", dispatch)
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": event_type,
            "data": {
                "role": "user",
                "content": [
                    {"type": block_type, "file_id": files[0].id, "filename": "renamed.txt"}
                ],
            },
        },
    )
    assert response.status_code == status, response.text
    if status == 415:
        assert filename in response.text
        assert "Claude Code or Codex" in response.text
        policy.assert_not_awaited()
    else:
        assert response.json()["denied"] is True
        policy.assert_awaited_once()
    dispatch.assert_not_awaited()
    assert conversations.list_items(session_id).data == before


def test_upload_rejects_oversized_filesystem_file(
    upload_client: tuple[TestClient, str],
) -> None:
    """A zip over the filesystem per-file cap is rejected with 413."""
    client, session_id = upload_client
    oversized = b"\x00" * (MAX_FILESYSTEM_ATTACHMENT_UPLOAD_BYTES + 1)
    resp = _upload(client, session_id, "huge.zip", oversized, "application/zip")
    assert resp.status_code == 413, resp.status_code


def test_upload_rejects_undecodable_oversized_image(
    upload_client: tuple[TestClient, str],
) -> None:
    """Image bytes over the model budget that don't decode are rejected 413.

    Real images are downscaled under the budget; garbage that only claims to
    be an image can't be compressed, so the route surfaces a 413 instead of
    storing an oversized attachment.
    """
    from omnigent.runtime.content_resolver import IMAGE_MODEL_BUDGET_BYTES

    client, session_id = upload_client
    oversized = b"\x00" * (IMAGE_MODEL_BUDGET_BYTES + 1)
    resp = _upload(client, session_id, "huge.png", oversized, "image/png")
    assert resp.status_code == 413, resp.status_code


def test_upload_large_image_is_compressed_under_budget(
    upload_client: tuple[TestClient, str],
) -> None:
    """A large but valid image uploads and is stored shrunk under the model budget."""
    import os
    from io import BytesIO

    from PIL import Image

    from omnigent.runtime.content_resolver import IMAGE_MODEL_BUDGET_BYTES

    client, session_id = upload_client
    side = 1600
    buffer = BytesIO()
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    assert len(payload) > IMAGE_MODEL_BUDGET_BYTES

    resp = _upload(client, session_id, "screenshot.png", payload, "image/png")
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["metadata"]["bytes"] <= IMAGE_MODEL_BUDGET_BYTES
    # Opaque image re-encodes (WebP preferred, JPEG fallback), so the stored
    # name is realigned to match the new type.
    assert body["name"] in ("screenshot.webp", "screenshot.jpg")


def test_upload_text_just_under_limit_succeeds(upload_client: tuple[TestClient, str]) -> None:
    """A text file just under the text cap is accepted."""
    client, session_id = upload_client
    payload = b"a" * (MAX_TEXT_UPLOAD_BYTES - 1024)
    resp = _upload(client, session_id, "big.txt", payload, "text/plain")
    assert resp.status_code in (200, 201), resp.status_code


@pytest.mark.parametrize("size,rejected", [(100, False), (101, True)])
async def test_read_upload_capped_boundary(size: int, rejected: bool) -> None:
    """The read cap admits the exact limit and rejects one byte more."""
    from io import BytesIO

    from fastapi import HTTPException, UploadFile

    from omnigent.server.routes.sessions import _read_upload_capped

    data = b"x" * size
    upload = UploadFile(file=BytesIO(data))
    if rejected:
        with pytest.raises(HTTPException) as error:
            await _read_upload_capped(upload, 100)
        assert error.value.status_code == 413
    else:
        assert await _read_upload_capped(upload, 100) == data


@pytest.mark.parametrize(
    "filename,content_type,status",
    [
        ("archive.zip", "application/zip", 415),
        ("archive.zip", "text/plain", 415),
        ("report.docx", "application/zip", 201),
    ],
)
def test_upload_denylist_uses_filename(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content_type: str,
    status: int,
) -> None:
    """The denylist rejects matching extensions regardless of the declared MIME."""
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_denied_extensions",
        lambda: frozenset({".zip"}),
    )
    client, session_id = upload_client
    response = _upload(client, session_id, filename, content_type=content_type)
    assert response.status_code == status, response.text
    if status == 415:
        assert "not accepted by this deployment" in response.text
    else:
        assert response.json()["name"] == filename


@pytest.mark.parametrize(
    "existing,limit,status",
    [(["a.zip", "b.zip"], 2, 413), (["a.txt", "b.txt", "c.txt"], 1, 201)],
)
def test_upload_quota_counts_only_filesystem_types(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
    existing: list[str],
    limit: int,
    status: int,
) -> None:
    """Only filesystem formats spend the quota across successive uploads."""
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit", lambda: limit
    )
    client, session_id = upload_client
    for filename in existing:
        response = _upload(client, session_id, filename)
        assert response.status_code == 201, response.text
    response = _upload(client, session_id, "bundle.zip")
    assert response.status_code == status, response.text
    if status == 413:
        assert "file attachments" in response.text


async def test_parallel_uploads_cannot_overspend_the_filesystem_quota(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two uploads racing for the last free slot: exactly one is stored."""
    import asyncio

    import httpx

    from omnigent.server.routes.sessions import routes_resources

    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit",
        lambda: 1,
    )
    real_read = routes_resources._read_upload_capped

    async def slow_read(file, limit):  # type: ignore[no-untyped-def]
        # Widen the gap between the quota check and the store.
        await asyncio.sleep(0.05)
        return await real_read(file, limit)

    monkeypatch.setattr(routes_resources, "_read_upload_capped", slow_read)
    client, session_id = upload_client
    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = await asyncio.gather(
            *(
                http.post(
                    f"/v1/sessions/{session_id}/resources/files",
                    files={"file": (f"race{i}.zip", b"PK\x03\x04 fake zip", "application/zip")},
                )
                for i in range(2)
            )
        )

    assert sorted(r.status_code for r in responses) == [201, 413]
    stored = SqlAlchemyFileStore(db_uri).list(session_id=session_id, limit=10).data
    assert [f.filename for f in stored if f.filename.endswith(".zip")] == [
        next(r.json()["name"] for r in responses if r.status_code == 201)
    ]


def test_quota_counts_filesystem_files_past_any_page_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota accounting walks beyond 20 pages of ordinary files."""
    from fastapi import HTTPException

    from omnigent.entities import StoredFile
    from omnigent.entities.pagination import PagedList
    from omnigent.server.routes._sessions.helpers import _enforce_filesystem_attachment_policy

    records = [
        StoredFile(id=f"f{i:03d}", created_at=i, filename=f"n{i}.txt", bytes=2) for i in range(30)
    ] + [StoredFile(id="f999", created_at=999, filename="one.zip", bytes=4)]

    class _OnePerPageStore:
        """Serves the session's files one record per page, oldest first."""

        def list(self, session_id: str, limit: int, after: str | None, order: str):
            del session_id, limit, order
            index = 0 if after is None else [r.id for r in records].index(after) + 1
            page = records[index : index + 1]
            return PagedList(
                data=page,
                first_id=page[0].id if page else None,
                last_id=page[-1].id if page else None,
                has_more=index + 1 < len(records),
            )

    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit",
        lambda: 1,
    )

    with pytest.raises(HTTPException) as exc:
        _enforce_filesystem_attachment_policy(
            ["two.zip"],
            session_id="conv_1",
            file_store=_OnePerPageStore(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 413


@pytest.mark.parametrize("filename", ["archive.zip", "report.docx", "state.sqlite"])
def test_old_host_refuses_new_types_without_storing(
    upload_client: tuple[TestClient, str], db_uri: str, filename: str
) -> None:
    """A legacy hello cannot promise cold resume; rejection leaves no file row."""
    client, session_id = upload_client
    client.app.state.host_registry.get("d75381f2c94b4e49a3c684946d4ddbc4").hello.capabilities = []
    response = _upload(client, session_id, filename, b"data", "application/octet-stream")
    assert response.status_code == 409, response.text
    assert "Update Omnigent" in response.text
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []


def test_old_host_keeps_existing_attachment_types(upload_client: tuple[TestClient, str]) -> None:
    """The upgrade requirement does not change existing text/image uploads."""
    from io import BytesIO

    from PIL import Image

    client, session_id = upload_client
    client.app.state.host_registry.get("d75381f2c94b4e49a3c684946d4ddbc4").hello.capabilities = []
    image = BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    for filename, data, mime in (
        ("table.csv", b"a,b\n1,2\n", "text/csv"),
        ("picture.png", image.getvalue(), "image/png"),
    ):
        response = _upload(client, session_id, filename, data, mime)
        assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_first_managed_upload_waits_for_host_binding(
    upload_client: tuple[TestClient, str], db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upload racing provisioning waits, then checks the newly bound host."""
    import asyncio

    import httpx

    from omnigent.server.managed_hosts import ManagedLaunchTracker
    from omnigent.server.routes.sessions import routes_resources

    client, session_id = upload_client
    store = SqlAlchemyConversationStore(db_uri)
    store.clear_host_binding(session_id)
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    client.app.state.managed_launches = tracker
    waiting = asyncio.Event()
    original = routes_resources._await_settled_managed_launch

    async def observe_wait(launch):
        waiting.set()
        await original(launch)

    monkeypatch.setattr(routes_resources, "_await_settled_managed_launch", observe_wait)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://test"
    ) as async_client:
        upload = asyncio.create_task(
            async_client.post(
                f"/v1/sessions/{session_id}/resources/files",
                files={"file": ("archive.zip", b"data", "application/zip")},
            )
        )
        await asyncio.wait_for(waiting.wait(), timeout=5)
        assert not upload.done()
        assert SqlAlchemyFileStore(db_uri).list(session_id).data == []
        store.set_host_id(
            session_id, "d75381f2c94b4e49a3c684946d4ddbc4", workspace="/tmp/test-upload"
        )
        tracker.finish(session_id)
        response = await upload
    assert response.status_code == 201, response.text


def test_recovered_host_ignores_stale_managed_launch_failure(
    upload_client: tuple[TestClient, str],
) -> None:
    """A retained failure does not block uploads after the session has recovered."""
    from omnigent.server.managed_hosts import ManagedLaunchTracker

    client, session_id = upload_client
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    tracker.fail(session_id, "earlier provision failed")
    client.app.state.managed_launches = tracker
    response = _upload(client, session_id, "archive.zip", b"data", "application/zip")
    assert response.status_code == 201, response.text


def test_unbound_upload_requires_a_connected_runtime(
    upload_client: tuple[TestClient, str],
    db_uri: str,
) -> None:
    """An unknown host cannot be assumed current just because the server is new."""
    client, session_id = upload_client
    SqlAlchemyConversationStore(db_uri).clear_host_binding(session_id)
    response = _upload(client, session_id, "archive.zip", b"data", "application/zip")
    assert response.status_code == 409, response.text
    assert "Connect an updated" in response.text
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []


def test_upload_wakes_a_sleeping_runtime_before_checking_support(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first attachment can wake a managed sandbox without requiring a text turn."""
    from unittest.mock import AsyncMock

    from omnigent.server.routes.sessions import routes_resources

    client, session_id = upload_client
    registry = client.app.state.host_registry
    connection = registry.get("d75381f2c94b4e49a3c684946d4ddbc4")
    registry.deregister(connection.host_id)

    async def wake(**kwargs):
        registry.register(connection.host_id, Mock(), connection.hello, owner=None)
        return None, SqlAlchemyConversationStore(db_uri).get_conversation(session_id)

    ensure = AsyncMock(side_effect=wake)
    monkeypatch.setattr(routes_resources, "ensure_runner_connected", ensure)
    response = _upload(client, session_id, "archive.zip", b"data", "application/zip")
    assert response.status_code == 201, response.text
    ensure.assert_awaited_once()


@pytest.mark.parametrize("partial_write", [False, True])
@pytest.mark.parametrize("filename", ["archive.zip", "notes.txt"])
def test_failed_upload_releases_quota_and_can_retry(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    partial_write: bool,
    filename: str,
) -> None:
    """Blob failures leave no metadata, partial bytes, or resource event behind."""
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit", lambda: 1
    )
    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_total_bytes_limit", lambda: 4
    )
    client, session_id = upload_client
    original_put = LocalArtifactStore.put
    attempted_ids: list[str] = []

    def fail_put(store: LocalArtifactStore, key: str, data: bytes) -> None:
        attempted_ids.append(key)
        if partial_write:
            original_put(store, key, data[:1])
        raise OSError("test storage write failure")

    url = f"/v1/sessions/{session_id}/resources/files"
    upload = {"file": (filename, b"data", "application/octet-stream")}
    with monkeypatch.context() as storage_failure:
        storage_failure.setattr(LocalArtifactStore, "put", fail_put)
        failed = client.post(url, files=upload)
    assert failed.status_code == 500, failed.text
    assert "Failed to upload file" in failed.text
    assert len(attempted_ids) == 1
    assert SqlAlchemyFileStore(db_uri).list(session_id).data == []
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    assert not artifacts.exists(attempted_ids[0])
    conversations = SqlAlchemyConversationStore(db_uri)
    assert conversations.list_items(session_id, type="resource_event").data == []

    retried = client.post(url, files=upload)
    assert retried.status_code == 201, retried.text
    assert artifacts.get(retried.json()["id"]) == b"data"
    files = SqlAlchemyFileStore(db_uri).list(session_id).data
    assert [stored.id for stored in files] == [retried.json()["id"]]
    assert len(conversations.list_items(session_id, type="resource_event").data) == 1
