"""Admission compatibility and hostile-name regression coverage."""

import uuid

import pytest
from fastapi import HTTPException

from omnigent.errors import OmnigentError
from omnigent.server.routes._sessions.helpers import (
    _classify_attachment_upload,
    _validate_attachment_content,
)
from omnigent.server.server_config import FilesystemAttachmentPolicy, filesystem_attachment_policy
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


def _policy(allowed=(), denied=()) -> FilesystemAttachmentPolicy:
    return FilesystemAttachmentPolicy(
        allowed_extensions="*" if allowed == "*" else frozenset(allowed),
        denied_extensions=frozenset(denied),
        max_bytes=1024,
        max_files=2,
        max_total_bytes=2048,
    )


@pytest.mark.parametrize("filename", ["clip.mp4", "payload.exe.png"])
def test_remote_image_names_cannot_bypass_admission(filename: str) -> None:
    with pytest.raises((HTTPException, OmnigentError)) as rejected:
        _validate_attachment_content(
            [
                {
                    "type": "input_image",
                    "filename": filename,
                    "image_url": f"https://example.com/{filename}",
                }
            ],
            session_id="a" * 32,
            file_store=None,
            policy=_policy([".mp4"], [".exe"]),
        )
    assert filename in str(rejected.value)


@pytest.mark.parametrize("allowed", [[], "*"])
@pytest.mark.parametrize(
    "filename,mime",
    [
        ("README", "text/plain"),
        ("Makefile", "text/plain"),
        ("Dockerfile", "text/plain"),
        ("photo", "image/png"),
        ("payload.bin", "text/plain"),
    ],
)
def test_declared_inline_mime_keeps_existing_admission(allowed, filename, mime) -> None:
    assert _classify_attachment_upload(filename, mime, _policy(allowed)) == (
        filename,
        mime,
        False,
    )


def test_inline_colon_name_does_not_require_filesystem_safety() -> None:
    assert _classify_attachment_upload("Screen 10:30.png", "image/png", _policy()) == (
        "Screen 10:30.png",
        "image/png",
        False,
    )
    with pytest.raises(HTTPException):
        _classify_attachment_upload("Screen 10:30.png", "image/png", _policy([".png"]))
    for name in ("clip.mp4:stream.txt", "clip.mp4\u00a0:stream.txt", "archive.zip:notes.txt"):
        with pytest.raises(HTTPException):
            _classify_attachment_upload(name, "text/plain", _policy([".mp4"]))


@pytest.mark.parametrize("filename", ["x." * 129, "é" * 128 + ".mp4"])
def test_filename_byte_limit_precedes_suffix_work(filename: str) -> None:
    with pytest.raises(HTTPException, match="255"):
        _classify_attachment_upload(filename, "video/mp4", _policy("*"))


@pytest.mark.parametrize("filename", ["a.exe\u00a0", "a.exe\u200b", "a.exe\u202e", "a\u2028.mp4"])
def test_unicode_names_cannot_evade_denied_suffix(filename: str) -> None:
    with pytest.raises(HTTPException):
        _classify_attachment_upload(filename, "video/mp4", _policy("*", [".exe"]))


@pytest.mark.parametrize("style", ["dashed", "legacy"])
def test_stored_reference_uses_store_id_normalization(db_uri: str, style: str) -> None:
    store = SqlAlchemyFileStore(db_uri)
    stored = store.create(filename="photo.png", bytes=1, content_type="image/png")
    alias = str(uuid.UUID(stored.id)) if style == "dashed" else f"file_{stored.id}"
    content = _validate_attachment_content(
        [{"type": "input_file", "file_id": alias, "file_data": "forged"}],
        session_id="a" * 32,
        file_store=store,
        policy=_policy(),
    )
    assert content == [{"type": "input_image", "file_id": stored.id, "filename": "photo.png"}]
    with pytest.raises(OmnigentError, match="not found"):
        _validate_attachment_content(
            [{"type": "input_file", "file_id": "file_abc"}],
            session_id="a" * 32,
            file_store=store,
            policy=_policy(),
        )


def test_unconfigured_file_store_retains_unresolved_reference() -> None:
    content = _validate_attachment_content(
        [{"type": "input_file", "file_id": "file_" + "a" * 32}],
        session_id="b" * 32,
        file_store=None,
        policy=_policy(),
    )
    assert content == [{"type": "input_file", "file_id": "file_" + "a" * 32}]


def test_supported_declared_mime_precedes_inline_filename_fallback() -> None:
    assert _classify_attachment_upload("calendar.ics", "image/png", _policy()) == (
        "calendar.ics",
        "image/png",
        False,
    )


@pytest.mark.parametrize(
    "filename,mime",
    [
        ("family👨‍👩.png", "image/png"),
        ("résumé\u00ad.pdf", "application/pdf"),
        ("שלום\u200f.txt", "text/plain"),
        ("\ufeffnote.txt", "text/plain"),
    ],
)
def test_format_characters_keep_display_names(filename, mime) -> None:
    assert _classify_attachment_upload(filename, mime, _policy()) == (filename, mime, False)


def test_format_characters_are_ignored_for_policy_matching() -> None:
    assert _classify_attachment_upload("clip.m\u200bp4", "text/plain", _policy([".mp4"])) == (
        "clip.m\u200bp4",
        "application/octet-stream",
        True,
    )
    for denied in ("a.exe ", "a.exe\u200b", "a.e\u200dxe"):
        with pytest.raises(HTTPException, match="not accepted"):
            _classify_attachment_upload(denied, "text/plain", _policy("*", [".exe"]))
    for control in ("\u202a", "\u202e", "\u2066", "\u2069"):
        with pytest.raises(HTTPException, match="control characters"):
            _classify_attachment_upload(f"a{control}.png", "image/png", _policy())


def test_padded_filename_is_bounded_before_character_processing(monkeypatch) -> None:
    from omnigent.inner import native_attachments

    calls = 0
    category = native_attachments.unicodedata.category

    def counted(char):
        nonlocal calls
        calls += 1
        return category(char)

    monkeypatch.setattr(native_attachments.unicodedata, "category", counted)
    with pytest.raises(ValueError, match="255-byte"):
        native_attachments.normalized_attachment_filename("clip.mp4" + " " * 200_000)
    assert calls == 0


def test_wildcard_declared_mime_steers_only_generic_names() -> None:
    for mime in ("text/plain", "image/png", "", "application/octet-stream"):
        for allowed in ("*", [".mp4"]):
            assert _classify_attachment_upload("clip.mp4", mime, _policy(allowed)) == (
                "clip.mp4",
                "application/octet-stream",
                True,
            )
            with pytest.raises(HTTPException, match="':' is forbidden"):
                _classify_attachment_upload("clip.mp4:x.png", mime, _policy(allowed))
    for filename, mime in (
        ("README", "text/plain"),
        ("Makefile", "text/plain"),
        ("Dockerfile", "text/plain"),
        ("photo", "image/png"),
        ("payload.bin", "text/plain"),
    ):
        for policy in (filesystem_attachment_policy({}), _policy("*")):
            assert _classify_attachment_upload(filename, mime, policy) == (filename, mime, False)
