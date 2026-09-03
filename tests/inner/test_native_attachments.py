"""Tests for the shared native-executor attachment helpers."""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import pytest

from omnigent.inner.native_attachments import (
    ATTACHMENT_MARKER_STRIP_PATTERN,
    MAX_SESSION_WORKSPACE_ATTACHMENTS,
    UNRESOLVED_ATTACHMENT_MARKER_PATTERN,
    WORKSPACE_ATTACHMENTS_DIRNAME,
    DataUri,
    attachment_reference_line,
    has_unresolved_file_id,
    materialize_attachment,
    materialize_attachment_to_workspace,
    parse_data_uri,
    resolve_file_id_block,
    unresolved_attachment_marker,
    workspace_attachment_reference_line,
    workspace_attachment_usage,
    workspace_materialize_upload_limit,
)

# A 1x1 transparent PNG, base64-encoded — small but a real decodable image.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)
_PNG_DATA_URI = f"data:image/png;base64,{_PNG_B64}"


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

    Writing through it would land the bytes wherever the link points —
    outside the workspace if an earlier turn planted the link.
    """
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    outside = tmp_path.parent / "outside-target.zip"
    (attachments_dir / "archive.zip").symlink_to(outside)

    assert materialize_attachment_to_workspace(_zip_block(), tmp_path) is None
    assert not outside.exists()


def test_materialize_to_workspace_enforces_file_count_quota(tmp_path: Path) -> None:
    """The file past the per-session count cap is rejected."""
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    for index in range(MAX_SESSION_WORKSPACE_ATTACHMENTS):
        (attachments_dir / f"existing_{index}.zip").write_bytes(b"x")

    assert materialize_attachment_to_workspace(_zip_block(), tmp_path) is None


def test_materialize_to_workspace_enforces_total_bytes_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file pushing the session over the byte cap is rejected."""
    monkeypatch.setattr(
        "omnigent.inner.native_attachments.MAX_SESSION_WORKSPACE_ATTACHMENT_BYTES",
        len(_ZIP_BYTES),
    )
    attachments_dir = tmp_path / WORKSPACE_ATTACHMENTS_DIRNAME
    attachments_dir.mkdir()
    (attachments_dir / "existing.zip").write_bytes(b"x")

    assert materialize_attachment_to_workspace(_zip_block(), tmp_path) is None


def test_materialize_to_workspace_reuses_identical_file(tmp_path: Path) -> None:
    """
    Re-materializing the same block reuses the file instead of consuming
    quota — the runner re-resolves history blocks after a relaunch, so a
    restart must not multiply a session's attachment footprint.
    """
    first = materialize_attachment_to_workspace(_zip_block(), tmp_path)
    second = materialize_attachment_to_workspace(_zip_block(), tmp_path)

    assert first == second
    assert workspace_attachment_usage(tmp_path) == (1, len(_ZIP_BYTES))


def test_workspace_attachment_usage_missing_dir_is_empty(tmp_path: Path) -> None:
    """A session with no materialized attachments reports zero usage."""
    assert workspace_attachment_usage(tmp_path) == (0, 0)


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

    resolved = await resolve_file_id_block(block, session_id="conv_1", client=_Client())

    assert resolved is not None
    assert resolved["file_data"] == _ZIP_DATA_URI
    assert workspace_materialize_upload_limit(str(resolved["filename"])) is not None
