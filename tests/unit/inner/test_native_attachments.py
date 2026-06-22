"""Tests for the shared native-executor attachment helpers."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from omnigent.inner.native_attachments import (
    DataUri,
    materialize_attachment,
    parse_data_uri,
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
