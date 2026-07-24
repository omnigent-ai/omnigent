"""Tests for the shared native-executor attachment helpers."""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import pytest

from omnigent.inner.native_attachments import (
    ATTACHMENT_MARKER_STRIP_PATTERN,
    UNRESOLVED_ATTACHMENT_MARKER_PATTERN,
    DataUri,
    attachment_reference_line,
    materialize_attachment,
    parse_data_uri,
    unresolved_attachment_marker,
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
