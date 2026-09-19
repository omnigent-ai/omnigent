"""Attachment upload type/size enforcement on POST /v1/sessions/{id}/resources/files."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT
from omnigent.inner.native_attachments import MAX_WORKSPACE_ATTACHMENT_UPLOAD_BYTES
from omnigent.runtime.content_resolver import (
    MAX_TEXT_UPLOAD_BYTES,
)
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
    # A Claude Code session, so workspace-delivered types are accepted.
    conversation_store.set_labels(conv.id, CLAUDE_NATIVE_CODING_AGENT.presentation_labels)

    app = FastAPI()

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
        ),
        prefix="/v1",
    )

    with TestClient(app) as client:
        yield client, conv.id


def test_upload_small_text_file_succeeds(upload_client: tuple[TestClient, str]) -> None:
    """A small text file uploads and returns a resource."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["name"] == "notes.txt"


def test_upload_rejects_unsupported_type(upload_client: tuple[TestClient, str]) -> None:
    """A type that is neither inlinable nor workspace-materializable is
    rejected with 415, not stored."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("clip.mp4", b"\x00\x00\x00 fake mp4 bytes", "video/mp4")},
    )
    assert resp.status_code == 415, resp.text
    assert "Unsupported attachment type" in resp.text


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("archive.zip", "application/zip"),
        (
            "deck.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        ("app.db", "application/octet-stream"),
    ],
)
def test_upload_accepts_workspace_materialize_types(
    upload_client: tuple[TestClient, str], filename: str, mime: str
) -> None:
    """Archives, office docs, and databases upload instead of 415ing, since a
    filesystem-capable harness reads them off disk rather than inlining them."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (filename, b"PK\x03\x04 fake bytes", mime)},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["name"] == filename


def test_upload_rejects_workspace_types_for_a_harness_without_a_workspace(
    upload_client: tuple[TestClient, str], db_uri: str
) -> None:
    """Only Claude Code and Codex open workspace files. Any other harness would
    receive the zip inlined and drop it, so the upload is refused up front."""
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
def test_message_cannot_inline_a_workspace_attachment(
    upload_client: tuple[TestClient, str], block: dict[str, str]
) -> None:
    """Inline bytes would reach the workspace without the upload route's checks."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "message", "data": {"role": "user", "content": [block]}},
    )
    assert resp.status_code == 400, resp.text
    assert "payload.zip" in resp.text


def test_upload_docx_mislabeled_as_zip_is_accepted(
    upload_client: tuple[TestClient, str],
) -> None:
    """Office formats are zip containers, so browsers routinely report them as
    application/zip. The extension decides, not the declared MIME."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("report.docx", b"PK\x03\x04 fake docx bytes", "application/zip")},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["name"] == "report.docx"


def test_upload_rejects_oversized_workspace_materialize_file(
    upload_client: tuple[TestClient, str],
) -> None:
    """A zip over the workspace-materialize per-file cap is rejected with 413."""
    client, session_id = upload_client
    oversized = b"\x00" * (MAX_WORKSPACE_ATTACHMENT_UPLOAD_BYTES + 1)
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("huge.zip", oversized, "application/zip")},
    )
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
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("huge.png", oversized, "image/png")},
    )
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

    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("screenshot.png", payload, "image/png")},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["metadata"]["bytes"] <= IMAGE_MODEL_BUDGET_BYTES
    # Opaque image re-encodes (WebP preferred, JPEG fallback), so the stored
    # name is realigned to match the new type.
    assert body["name"] in ("screenshot.webp", "screenshot.jpg")


def test_upload_csv_mislabeled_as_excel_is_accepted(
    upload_client: tuple[TestClient, str],
) -> None:
    """A .csv the browser tags application/vnd.ms-excel is accepted via the
    extension fallback and stored as a text type (parity with the web client)."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("data.csv", b"a,b,c\n1,2,3\n", "application/vnd.ms-excel")},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["name"] == "data.csv"


def test_upload_text_just_under_limit_succeeds(upload_client: tuple[TestClient, str]) -> None:
    """A text file just under the text cap is accepted."""
    client, session_id = upload_client
    payload = b"a" * (MAX_TEXT_UPLOAD_BYTES - 1024)
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("big.txt", payload, "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.status_code


class _FakeUpload:
    """Minimal UploadFile stand-in exposing the chunked ``read`` interface."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, size: int) -> bytes:
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


async def test_read_upload_capped_allows_exactly_at_limit() -> None:
    """A payload exactly at the limit is accepted (the ``>`` boundary)."""
    from omnigent.server.routes.sessions import _read_upload_capped

    data = b"x" * 100
    assert await _read_upload_capped(_FakeUpload(data), 100) == data


async def test_read_upload_capped_rejects_one_over_limit() -> None:
    """One byte over the limit raises HTTP 413."""
    import pytest as _pytest
    from fastapi import HTTPException

    from omnigent.server.routes.sessions import _read_upload_capped

    with _pytest.raises(HTTPException) as exc_info:
        await _read_upload_capped(_FakeUpload(b"x" * 101), 100)
    assert exc_info.value.status_code == 413


def test_upload_rejects_an_extension_the_deployment_denies(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An operator can narrow the built-in allowlist without a code change.

    Without this a deployment that must not accept archives has no way to
    refuse them short of forking, which is what the issue's "allow deployments
    to deny selected MIME types or extensions" requirement is for.
    """
    monkeypatch.setattr(
        "omnigent.server.server_config.workspace_attachment_denied_extensions",
        lambda: frozenset({".zip"}),
    )
    client, session_id = upload_client

    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("archive.zip", b"PK\x03\x04 fake zip", "application/zip")},
    )

    assert resp.status_code == 415, resp.text
    assert "not accepted by this deployment" in resp.text


def test_upload_still_accepts_a_type_the_denylist_does_not_name(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The denylist narrows the allowlist precisely, not wholesale."""
    monkeypatch.setattr(
        "omnigent.server.server_config.workspace_attachment_denied_extensions",
        lambda: frozenset({".zip"}),
    )
    client, session_id = upload_client

    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("report.docx", b"PK\x03\x04 fake docx", "application/zip")},
    )

    assert resp.status_code in (200, 201), resp.text


def test_upload_rejects_once_the_session_file_quota_is_spent(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The per-session file count is enforced across uploads, not per request.

    Counting only the current upload would let a session accumulate unbounded
    files in the sandbox one request at a time.
    """
    monkeypatch.setattr(
        "omnigent.server.server_config.workspace_attachment_file_limit",
        lambda: 2,
    )
    client, session_id = upload_client

    for i in range(2):
        ok = client.post(
            f"/v1/sessions/{session_id}/resources/files",
            files={"file": (f"a{i}.zip", b"PK\x03\x04 fake zip", "application/zip")},
        )
        assert ok.status_code in (200, 201), ok.text

    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("third.zip", b"PK\x03\x04 fake zip", "application/zip")},
    )

    assert resp.status_code == 413, resp.text
    assert "workspace attachments" in resp.text


async def test_parallel_uploads_cannot_overspend_the_workspace_quota(
    upload_client: tuple[TestClient, str],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two uploads racing for the last free slot: exactly one is stored."""
    import asyncio

    import httpx

    from omnigent.server.routes.sessions import routes_resources

    monkeypatch.setattr(
        "omnigent.server.server_config.workspace_attachment_file_limit",
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


def test_inlined_attachments_do_not_spend_the_workspace_quota(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Images and text never reach the sandbox filesystem, so they must not
    count against a quota that exists to bound sandbox disk use.
    """
    monkeypatch.setattr(
        "omnigent.server.server_config.workspace_attachment_file_limit",
        lambda: 1,
    )
    client, session_id = upload_client

    for name in ("a.txt", "b.txt", "c.txt"):
        filler = client.post(
            f"/v1/sessions/{session_id}/resources/files",
            files={"file": (name, b"hello", "text/plain")},
        )
        assert filler.status_code in (200, 201), filler.text

    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("bundle.zip", b"PK\x03\x04 fake zip", "application/zip")},
    )

    assert resp.status_code in (200, 201), resp.text


def test_declared_text_mime_cannot_skip_the_workspace_policy(
    upload_client: tuple[TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A zip sent as ``text/plain`` still goes through the workspace checks.

    Delivery follows the filename, so trusting the declared MIME would store
    the archive as inline text, skip the denylist and quota, and still have
    the executor write it into the workspace.
    """
    monkeypatch.setattr(
        "omnigent.server.server_config.workspace_attachment_denied_extensions",
        lambda: frozenset({".zip"}),
    )
    client, session_id = upload_client

    resp = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("archive.zip", b"PK\x03\x04 fake zip", "text/plain")},
    )

    assert resp.status_code == 415, resp.text
    assert "not accepted by this deployment" in resp.text


def test_quota_counts_workspace_files_past_any_page_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Workspace files are counted however many inline files precede them.

    Stopping after a fixed number of pages let a session bury workspace
    uploads behind enough inline ones and exceed its configured limit. The
    fake store serves one record per page so the zip sits past the point where
    the old 20-page scan stopped.
    """
    from fastapi import HTTPException

    from omnigent.entities import StoredFile
    from omnigent.entities.pagination import PagedList
    from omnigent.server.routes._sessions.helpers import _enforce_workspace_attachment_policy

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
        "omnigent.server.server_config.workspace_attachment_file_limit",
        lambda: 1,
    )

    with pytest.raises(HTTPException) as exc:
        _enforce_workspace_attachment_policy(
            ["two.zip"],
            session_id="conv_1",
            file_store=_OnePerPageStore(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 413
