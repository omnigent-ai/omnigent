"""Tests for the shared native-executor attachment helpers."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.native_attachments import (
    ATTACHMENT_MARKER_STRIP_PATTERN,
    UNRESOLVED_ATTACHMENT_MARKER_PATTERN,
    WORKSPACE_ATTACHMENTS_DIRNAME,
    DataUri,
    attachment_reference_line,
    codex_resize_metadata_path,
    has_unresolved_file_id,
    materialize_attachment,
    materialize_attachment_to_workspace,
    parse_data_uri,
    resize_notice,
    resolve_file_id_block,
    routed_attachment_reference_line,
    unresolved_attachment_marker,
    workspace_attachment_reference_line,
    workspace_materialize_upload_limit,
)

# A 1x1 transparent PNG, base64-encoded — small but a real decodable image.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)
_PNG_DATA_URI = f"data:image/png;base64,{_PNG_B64}"


def test_resize_alias_copy_failure_leaves_no_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "photo.png"
    path.write_bytes(base64.b64decode(_PNG_B64))
    dimensions = {"width": 6000, "height": 4000}

    def fail_copy(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes()[:8])
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("omnigent.inner.native_attachments.shutil.copyfile", fail_copy)
        assert codex_resize_metadata_path(path, dimensions) == path
    assert list(tmp_path.iterdir()) == [path]
    alias = codex_resize_metadata_path(path, dimensions)
    assert alias != path
    assert alias.read_bytes() == path.read_bytes()
    assert codex_resize_metadata_path(path, dimensions) == alias


def test_parse_data_uri_splits_mime_and_payload() -> None:
    """
    parse_data_uri returns the MIME type and base64 payload separately.

    Proves the header is stripped of both the ``data:`` prefix and the
    ``;base64`` suffix so callers get a clean MIME type. A failure here
    means downstream extension/MIME logic would key off a malformed
    string and pick the wrong file extension.
    """
    parsed = parse_data_uri(_PNG_DATA_URI)

    assert parsed == DataUri(mime_type="image/png", base64_payload=_PNG_B64)


def test_parse_data_uri_without_comma_raises() -> None:
    """
    parse_data_uri rejects a URI that has no comma separator.

    A failure (no raise) would mean a malformed URI silently yields an
    empty payload and a later base64 decode produces empty bytes
    instead of surfacing the bad input.
    """
    with pytest.raises(ValueError, match="no comma separator"):
        parse_data_uri("data:image/png;base64")


def test_materialize_attachment_writes_decoded_bytes(tmp_path: Path) -> None:
    """
    An image block is decoded and written under ``uploads/``.

    Proves the bytes written are the decoded PNG (not the base64 text),
    so a Codex ``localImage`` path or a Claude ``[Attached: ...]``
    reference points at a real, openable image. A failure means the
    attachment never reached disk and the model would see nothing.
    """
    block = {"type": "input_image", "image_url": _PNG_DATA_URI}

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.parent == tmp_path / "uploads"
    assert path.read_bytes() == base64.b64decode(_PNG_B64)
    assert path.suffix == ".png"  # MIME-derived extension when no filename given


def test_materialize_attachment_uses_block_filename(tmp_path: Path) -> None:
    """
    A supplied filename is honored (basename only, to avoid traversal).

    Proves a caller-provided ``filename`` is used for the on-disk name
    but stripped to its basename. A failure here would either lose the
    user's filename or, worse, let ``../`` components escape the
    uploads directory.
    """
    block = {
        "type": "input_image",
        "image_url": _PNG_DATA_URI,
        "filename": "../../evil.png",
    }

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.name == "evil.png"
    assert path.parent == tmp_path / "uploads"


def test_materialize_attachment_ignores_non_string_filename(tmp_path: Path) -> None:
    block = {
        "type": "input_image",
        "image_url": _PNG_DATA_URI,
        "filename": 42,
    }

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.name.startswith("attachment_")
    assert path.suffix == ".png"


def test_materialize_attachment_returns_none_without_data_uri(tmp_path: Path) -> None:
    """
    A block whose data URI is missing yields ``None`` and writes nothing.

    Proves an unresolved attachment (e.g. a bare ``file_id`` the content
    resolver never filled in) is skipped rather than crashing. A failure
    would surface as an exception mid-turn or an empty file on disk.
    """
    block = {"type": "input_image", "file_id": "file_unresolved"}

    path = materialize_attachment(block, tmp_path)

    assert path is None
    assert not (tmp_path / "uploads").exists()


def test_materialize_attachment_unresolved_file_id_logs_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    An unresolved ``file_id`` block is logged at ERROR, not WARNING.

    The block reaching an executor unresolved means the attachment is
    about to be lost for the whole turn; a warning was too quiet for a
    failure whose user-visible symptom is a hallucinated attachment.
    """
    block = {"type": "input_image", "file_id": "file_unresolved"}

    with caplog.at_level(logging.ERROR, logger="omnigent.inner.native_attachments"):
        path = materialize_attachment(block, tmp_path)

    assert path is None
    records = [
        record
        for record in caplog.records
        if "unresolved file_id file_unresolved" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


def test_unresolved_attachment_marker_names_the_attachment() -> None:
    """
    The marker names the attachment by filename, falling back to file_id.

    Proves the placeholder callers emit for a failed attachment tells
    the model (and the user, via the mirrored transcript) WHICH file was
    lost, instead of the attachment silently vanishing.
    """
    named = {"type": "input_image", "file_id": "file_x", "filename": "photo.png"}
    unnamed = {"type": "input_image", "file_id": "file_x"}
    bare = {"type": "input_image"}

    assert unresolved_attachment_marker(named) == "[Attachment photo.png could not be loaded]"
    assert unresolved_attachment_marker(unnamed) == "[Attachment file_x could not be loaded]"
    assert unresolved_attachment_marker(bare) == "[Attachment attachment could not be loaded]"


def test_unresolved_attachment_marker_sanitizes_bracketed_names() -> None:
    """
    Brackets and newlines in the filename cannot break the marker shape.

    Consumers (title synthesis, TUI forwarders) match the marker via
    UNRESOLVED_ATTACHMENT_MARKER_PATTERN; an unsanitized ``]`` in the
    name would end their match early and leak marker fragments into
    titles and mirrored chat bubbles.
    """
    bracketed = unresolved_attachment_marker(
        {"type": "input_image", "filename": "shot [final].png"}
    )
    multiline = unresolved_attachment_marker({"type": "input_image", "filename": "a\nb.png"})

    assert bracketed == "[Attachment shot _final_.png could not be loaded]"
    assert re.fullmatch(UNRESOLVED_ATTACHMENT_MARKER_PATTERN, bracketed)
    assert re.fullmatch(UNRESOLVED_ATTACHMENT_MARKER_PATTERN, multiline)


def test_materialize_attachment_reuses_identical_existing_file(tmp_path: Path) -> None:
    """
    Re-materializing identical bytes returns the existing file.

    History replays re-materialize the same blocks on every resume;
    without content-equal dedupe the uploads dir would grow a suffixed
    copy per resume. Different bytes under the same name still get a
    fresh suffixed path.
    """
    block = {"type": "input_image", "image_url": _PNG_DATA_URI, "filename": "photo.png"}
    other_payload = base64.b64encode(b"other-bytes").decode()
    other = {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{other_payload}",
        "filename": "photo.png",
    }

    first = materialize_attachment(block, tmp_path)
    second = materialize_attachment(block, tmp_path)
    third = materialize_attachment(other, tmp_path)

    assert first is not None
    assert second == first
    assert third is not None and third != first
    assert len(list((tmp_path / "uploads").iterdir())) == 2


def test_materialize_attachment_same_name_collision_is_bounded(tmp_path: Path) -> None:
    """
    Same-named attachments with different bytes stay bounded across rebuilds.

    A transcript that carries two distinct ``image.png`` uploads is
    re-materialized on every runner restart. A randomized collision path
    would hand the second attachment a fresh name each rebuild and grow
    ``uploads/`` without bound; the collision path must be derived from
    the content so each distinct payload keeps exactly one file.
    """
    first_payload = base64.b64encode(b"first-image-bytes").decode()
    second_payload = base64.b64encode(b"second-image-bytes").decode()
    first_block = {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{first_payload}",
        "filename": "image.png",
    }
    second_block = {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{second_payload}",
        "filename": "image.png",
    }

    rebuilds = [
        (
            materialize_attachment(first_block, tmp_path),
            materialize_attachment(second_block, tmp_path),
        )
        for _ in range(4)
    ]

    uploads = tmp_path / "uploads"
    assert len(list(uploads.iterdir())) == 2
    # Every rebuild resolves to the same pair of paths.
    assert all(pair == rebuilds[0] for pair in rebuilds)
    assert rebuilds[0][0] != rebuilds[0][1]
    assert rebuilds[0][0].read_bytes() == base64.b64decode(first_payload)
    assert rebuilds[0][1].read_bytes() == base64.b64decode(second_payload)


def test_materialize_attachment_sanitizes_bracketed_filenames(tmp_path: Path) -> None:
    """
    Brackets in the filename cannot break the "[Attached: ...]" line.

    The success-path reference line is matched by the same consumers as
    the unresolved marker; an unsanitized ``]`` in the written path
    would end their ``\\[Attached:[^\\]]*\\]`` match early.
    """
    block = {
        "type": "input_image",
        "image_url": _PNG_DATA_URI,
        "filename": "shot [final].png",
    }

    path = materialize_attachment(block, tmp_path)

    assert path is not None
    assert path.name == "shot _final_.png"


def test_attachment_reference_line_covers_both_outcomes(tmp_path: Path) -> None:
    """
    One call site yields the path line or the visible loss marker.

    Both shapes must match ATTACHMENT_MARKER_STRIP_PATTERN so TUI
    forwarders can strip them from mirrored bubbles.
    """
    resolved = {"type": "input_image", "image_url": _PNG_DATA_URI, "filename": "photo.png"}
    unresolved = {"type": "input_image", "file_id": "file_x", "filename": "photo.png"}

    resolved_line = attachment_reference_line(resolved, tmp_path)
    unresolved_line = attachment_reference_line(unresolved, tmp_path)

    assert resolved_line == f"[Attached: {tmp_path / 'uploads' / 'photo.png'}]"
    assert unresolved_line == "[Attachment photo.png could not be loaded]"
    assert re.fullmatch(ATTACHMENT_MARKER_STRIP_PATTERN, resolved_line)
    assert re.fullmatch(ATTACHMENT_MARKER_STRIP_PATTERN, unresolved_line)


# ── Workspace materialization ────────────────────────────────────────


_ZIP_BYTES = b"PK\x03\x04 not really a zip"
_ZIP_DATA_URI = f"data:application/zip;base64,{base64.b64encode(_ZIP_BYTES).decode()}"


def _zip_block(filename: str = "archive.zip") -> dict[str, object]:
    """Build a resolved input_file block for a zip attachment."""
    return {"type": "input_file", "file_data": _ZIP_DATA_URI, "filename": filename}


def test_materialize_to_workspace_writes_under_attachments_dir(tmp_path: Path) -> None:
    """
    The decoded bytes land in the workspace's session-attachments directory.

    This is what makes the file reachable by the harness's own Read/Bash
    tools: the workspace is its cwd, so no sandbox exception is needed.
    """
    path = materialize_attachment_to_workspace(_zip_block(), tmp_path)

    assert path == tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME / "archive.zip"
    assert path.read_bytes() == _ZIP_BYTES


def test_materialize_to_workspace_strips_executable_bits(tmp_path: Path) -> None:
    """
    A materialized file is never executable.

    Uploads are untrusted input; leaving the execute bit set (from a
    permissive umask) would let an attached binary be run directly in the
    sandbox rather than merely read.
    """
    path = materialize_attachment_to_workspace(_zip_block("payload.zip"), tmp_path)

    assert path is not None
    assert path.stat().st_mode & 0o111 == 0


def test_materialize_to_workspace_contains_path_traversal(tmp_path: Path) -> None:
    """
    A traversal filename is written inside the attachments dir, not above it.

    Failure would let an upload overwrite arbitrary files in the workspace
    (or outside it) by name alone.
    """
    path = materialize_attachment_to_workspace(_zip_block("../../escaped.zip"), tmp_path)

    assert path is not None
    assert path.parent == tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    assert not (tmp_path.parent / "escaped.zip").exists()


def test_materialize_to_workspace_refuses_symlinked_destination(tmp_path: Path) -> None:
    """
    An existing symlink at the destination is refused, not followed.

    Writing through it would land the bytes wherever the link points,
    outside the workspace if an earlier turn planted the link.
    """
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    outside = tmp_path.parent / "outside-target.zip"
    (attachments_dir / "archive.zip").symlink_to(outside)

    assert materialize_attachment_to_workspace(_zip_block(), tmp_path) is None
    assert not outside.exists()


def test_materialize_to_workspace_refuses_symlink_at_collision_name(tmp_path: Path) -> None:
    """
    A symlink planted at the digest-suffixed collision name is refused.

    The original name is taken by other content, which diverts the write to
    ``<stem>_<sha12><suffix>``. That name is predictable, so a link placed
    there must not redirect the write onto a file outside the workspace.
    """
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    (attachments_dir / "archive.zip").write_bytes(b"different content")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.zip"
    outside.write_bytes(b"precious")
    digest = hashlib.sha256(_ZIP_BYTES).hexdigest()[:12]
    (attachments_dir / f"archive_{digest}.zip").symlink_to(outside)

    assert materialize_attachment_to_workspace(_zip_block(), tmp_path) is None
    assert outside.read_bytes() == b"precious"


def test_materialize_to_workspace_refuses_symlinked_attachments_dir(tmp_path: Path) -> None:
    """A symlinked attachments directory is refused rather than written through."""
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    (tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME).symlink_to(elsewhere, target_is_directory=True)

    assert materialize_attachment_to_workspace(_zip_block(), tmp_path) is None
    assert list(elsewhere.iterdir()) == []


def test_materialize_to_workspace_does_not_overwrite_when_both_names_taken(
    tmp_path: Path,
) -> None:
    """With the original and collision names holding other content, nothing is overwritten."""
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    digest = hashlib.sha256(_ZIP_BYTES).hexdigest()[:12]
    (attachments_dir / "archive.zip").write_bytes(b"first")
    (attachments_dir / f"archive_{digest}.zip").write_bytes(b"second")

    assert materialize_attachment_to_workspace(_zip_block(), tmp_path) is None
    assert (attachments_dir / "archive.zip").read_bytes() == b"first"
    assert (attachments_dir / f"archive_{digest}.zip").read_bytes() == b"second"


def test_materialize_to_workspace_reuses_identical_file(tmp_path: Path) -> None:
    """
    Re-materializing the same block reuses the file. The runner re-resolves
    history blocks after a relaunch, so a restart must not multiply copies.
    """
    first = materialize_attachment_to_workspace(_zip_block(), tmp_path)
    second = materialize_attachment_to_workspace(_zip_block(), tmp_path)

    assert first == second
    assert len(list((tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME).iterdir())) == 1


def test_materialize_to_workspace_clears_executable_bits_on_reuse(tmp_path: Path) -> None:
    """
    An identical file already present with execute bits is reused non-executable.

    Reuse returns early, so without clearing the bits there a pre-placed
    executable copy would stay runnable despite the attachment contract.
    """
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    existing = attachments_dir / "archive.zip"
    existing.write_bytes(_ZIP_BYTES)
    existing.chmod(0o755)

    path = materialize_attachment_to_workspace(_zip_block(), tmp_path)

    assert path == existing
    assert path.stat().st_mode & 0o111 == 0


def test_materialize_to_workspace_ignores_files_left_by_other_sessions(tmp_path: Path) -> None:
    """
    Files other sessions left in a shared workspace do not block a new upload.

    One checkout serves many sessions, so the runner cannot tell whose files
    these are; per-session quotas are enforced by the server at upload.
    """
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    for index in range(50):
        (attachments_dir / f"left_behind_{index}.zip").write_bytes(b"x" * 1024)

    path = materialize_attachment_to_workspace(_zip_block(), tmp_path)

    assert path == attachments_dir / "archive.zip"


def test_routed_reference_line_sends_each_type_to_its_destination(tmp_path: Path) -> None:
    """
    Live turns and transcript rebuilds share one router, so a zip goes to the
    workspace and an image to the bridge dir on both paths.
    """
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    image = {"type": "input_image", "image_url": _PNG_DATA_URI, "filename": "photo.png"}

    zip_line = routed_attachment_reference_line(_zip_block(), bridge_dir, workspace)
    image_line = routed_attachment_reference_line(image, bridge_dir, workspace)
    no_workspace = routed_attachment_reference_line(_zip_block(), bridge_dir, None)

    assert (
        zip_line == f"[Attached file: {workspace / WORKSPACE_ATTACHMENTS_DIRNAME / 'archive.zip'}]"
    )
    assert image_line == f"[Attached: {bridge_dir / 'uploads' / 'photo.png'}]"
    assert no_workspace == "[Attachment archive.zip could not be loaded]"


def test_workspace_reference_line_covers_both_outcomes(tmp_path: Path) -> None:
    """
    The workspace line uses the "[Attached file: ...]" shape and, like the
    bridge-dir line, both outcomes stay strippable by TUI forwarders.
    """
    unresolved = {"type": "input_file", "file_id": "file_x", "filename": "archive.zip"}

    resolved_line = workspace_attachment_reference_line(_zip_block(), tmp_path)
    unresolved_line = workspace_attachment_reference_line(unresolved, tmp_path)

    expected = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME / "archive.zip"
    assert resolved_line == f"[Attached file: {expected}]"
    assert unresolved_line == "[Attachment archive.zip could not be loaded]"
    assert re.fullmatch(ATTACHMENT_MARKER_STRIP_PATTERN, resolved_line)
    assert re.fullmatch(ATTACHMENT_MARKER_STRIP_PATTERN, unresolved_line)


def test_workspace_reference_line_matches_title_seeding_regex(tmp_path: Path) -> None:
    """
    The emitted line matches conversation.py's marker regex, so a session
    started with an attachment is titled by what the user typed rather than
    by a workspace path echoed back through the harness transcript.
    """
    from omnigent.entities.conversation import _ATTACHMENT_MARKER_RE

    line = workspace_attachment_reference_line(_zip_block(), tmp_path)

    assert _ATTACHMENT_MARKER_RE.fullmatch(line)


@pytest.mark.parametrize(
    "filename", ["archive.zip", "report.docx", "sheet.xlsx", "deck.pptx", "app.sqlite3"]
)
def test_workspace_materialize_upload_limit_allowed(filename: str) -> None:
    """Archives, office documents, and databases are workspace-delivered."""
    assert workspace_materialize_upload_limit(filename) is not None


@pytest.mark.parametrize("filename", ["photo.png", "report.pdf", "notes.txt", "blob", None])
def test_workspace_materialize_upload_limit_rejects_inlinable(filename: str | None) -> None:
    """Inlinable and unrecognised types keep the existing inline delivery."""
    assert workspace_materialize_upload_limit(filename) is None


async def test_relaunch_re_resolution_keeps_a_zip_routable_to_the_workspace() -> None:
    """
    After a runner relaunch, history is reloaded pre-resolution and each
    ``file_id`` block is fetched again. The rebuilt block must keep the
    filename, or the executor loses the signal that routes it to the
    workspace and would stage it in the bridge dir instead.
    """

    class _Resp:
        """Minimal httpx-Response stand-in for metadata and content."""

        def __init__(self, *, body: bytes = b"", payload: dict[str, object] | None = None) -> None:
            self.content = body
            self._payload = payload or {}
            self.headers = {"content-type": "application/zip"}

        def json(self) -> dict[str, object]:
            return self._payload

        def raise_for_status(self) -> None:
            return

    class _Client:
        """Serves the two GETs re-resolution makes per attachment."""

        async def get(self, url: str, **kwargs: object) -> _Resp:
            del kwargs
            if url.endswith("/content"):
                return _Resp(body=_ZIP_BYTES)
            return _Resp(payload={"filename": "bundle.zip", "content_type": "application/zip"})

    block = {"type": "input_file", "file_id": "file_zip", "filename": "bundle.zip"}
    assert has_unresolved_file_id(block)

    result = await resolve_file_id_block(block, session_id="conv_1", client=_Client())

    assert result is not None
    resolved, _notice = result
    assert resolved["file_data"] == _ZIP_DATA_URI
    assert workspace_materialize_upload_limit(str(resolved["filename"])) is not None


async def test_re_resolution_takes_the_filename_from_stored_metadata() -> None:
    """
    A client cannot relabel an uploaded file by naming it differently in the message.

    ``payload.txt`` passed the upload gate as inline text. Referencing it as
    ``payload.db`` would otherwise route it to the workspace, skipping the
    denylist and quota that apply to workspace uploads.
    """

    class _Resp:
        """Minimal httpx-Response stand-in for metadata and content."""

        def __init__(self, *, body: bytes = b"", payload: dict[str, object] | None = None) -> None:
            self.content = body or b"{}"
            self._payload = payload or {}
            self.headers = {"content-type": "text/plain"}

        def json(self) -> dict[str, object]:
            return self._payload

        def raise_for_status(self) -> None:
            return

    class _Client:
        """Serves the stored metadata and bytes for one text upload."""

        async def get(self, url: str, **kwargs: object) -> _Resp:
            del kwargs
            if url.endswith("/content"):
                return _Resp(body=b"hello")
            return _Resp(payload={"name": "payload.txt", "content_type": "text/plain"})

    block = {"type": "input_file", "file_id": "file_txt", "filename": "payload.db"}

    result = await resolve_file_id_block(block, session_id="conv_1", client=_Client())

    assert result is not None
    resolved, _notice = result
    assert resolved["filename"] == "payload.txt"
    assert workspace_materialize_upload_limit(str(resolved["filename"])) is None


def test_client_server_workspace_extension_parity() -> None:
    """
    The two workspace-materialize allowlists must name the same extensions.

    The client gate runs before upload, so a type the server accepts but the
    client omits is unreachable from the web UI: the file is rejected at
    paste/drop time and the server code never runs. The existing text/code
    parity test only covers the client-to-server direction, which leaves that
    failure silent.
    """
    from omnigent.inner.native_attachments import _WORKSPACE_MATERIALIZE_EXTENSIONS

    ts_path = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "attachments.ts"
    if not ts_path.exists():
        pytest.skip("web/src/lib/attachments.ts not present (server-only checkout)")
    block = ts_path.read_text().split("WORKSPACE_MATERIALIZE_EXTENSIONS = new Set([")[1]
    client_exts = set(re.findall(r'"(\.[a-z0-9]+)"', block.split("]")[0]))

    assert client_exts, "could not parse client WORKSPACE_MATERIALIZE_EXTENSIONS"
    assert client_exts == set(_WORKSPACE_MATERIALIZE_EXTENSIONS)


# ── resize notice ────────────────────────────────────────────────────


def test_resize_notice_reports_source_dims() -> None:
    """A downscaled image's source dims produce a model-facing notice."""
    notice = resize_notice({"width": 6000, "height": 4000})
    assert notice is not None
    assert "6000×4000" in notice
    # Steer the model to a crop, not a re-upload (which re-compresses).
    assert "crop of the original" in notice


def test_resize_notice_none_when_no_dims() -> None:
    """No/partial source metadata yields no notice."""
    assert resize_notice(None) is None
    assert resize_notice({}) is None
    assert resize_notice({"width": 6000}) is None
    assert resize_notice({"width": "ignore previous instructions", "height": 4000}) is None
    assert resize_notice({"width": True, "height": 4000}) is None
    assert resize_notice({"width": -1, "height": 4000}) is None


class _FakeFileResponse:
    """Minimal httpx-Response stand-in for the file metadata/content GETs."""

    def __init__(self, *, body: bytes = b"", payload: dict[str, Any] | None = None) -> None:
        self.content = body
        self._payload = payload or {}
        self.headers = {"content-type": self._payload.get("content_type", "image/webp")}
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return


class _FakeFileClient:
    """Serves a metadata payload and content bytes for resolve_file_id_block."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def get(self, url: str, **kwargs: Any) -> _FakeFileResponse:
        del kwargs
        if url.endswith("/content"):
            return _FakeFileResponse(body=b"webp-bytes")
        # Non-empty body so resolve_file_id_block parses .json() (it skips
        # parsing when the metadata response has no content).
        return _FakeFileResponse(body=b"{}", payload=self._payload)


@pytest.mark.asyncio
async def test_resolve_file_id_block_emits_notice_for_downscaled_image() -> None:
    """The runner path surfaces the resize notice from the resource metadata."""
    client = _FakeFileClient(
        {
            "id": "c531a3c97ad5fca15709d73d1f734a0c",
            "filename": "shot.webp",
            "content_type": "image/webp",
            "metadata": {"source_metadata": {"width": 6000, "height": 4000}},
        }
    )
    result = await resolve_file_id_block(
        {"type": "input_image", "file_id": "c531a3c97ad5fca15709d73d1f734a0c"},
        session_id="405bfe154d5c0e795a2b87021bc897bf",
        client=client,  # type: ignore[arg-type]
    )
    assert result is not None
    new_block, notice = result
    assert new_block["image_url"].startswith("data:image/webp;base64,")
    assert "file_id" not in new_block
    assert notice == {"width": 6000, "height": 4000}


@pytest.mark.asyncio
async def test_resolve_file_id_block_no_notice_without_source_metadata() -> None:
    """A non-downscaled image resolves with no notice."""
    client = _FakeFileClient(
        {
            "id": "c531a3c97ad5fca15709d73d1f734a0c",
            "filename": "a.webp",
            "content_type": "image/webp",
        }
    )
    result = await resolve_file_id_block(
        {"type": "input_image", "file_id": "c531a3c97ad5fca15709d73d1f734a0c"},
        session_id="405bfe154d5c0e795a2b87021bc897bf",
        client=client,  # type: ignore[arg-type]
    )
    assert result is not None
    _, notice = result
    assert notice is None
