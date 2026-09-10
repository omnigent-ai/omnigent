"""Attachment policy: what gets forwarded, and what gets refused and why."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from omnigent_discord import attachments as attachments_module
from omnigent_discord.attachments import AttachmentPolicy, collect

pytestmark = pytest.mark.asyncio


class FakeAttachment:
    """Stands in for ``discord.Attachment``: a name, a size, and a save()."""

    def __init__(self, filename: str, payload: bytes = b"data", size: int | None = None) -> None:
        self.filename = filename
        self.payload = payload
        self.size = len(payload) if size is None else size
        self.saved_to: Path | None = None
        self.attempts = 0

    async def save(self, destination: Any, **_kwargs: Any) -> None:
        self.attempts += 1
        self.saved_to = Path(destination)
        Path(destination).write_bytes(self.payload)


class ExplodingAttachment(FakeAttachment):
    """An attachment whose download always fails, origin and proxy alike."""

    async def save(self, destination: Any, **_kwargs: Any) -> None:
        self.attempts += 1
        raise OSError("cdn unavailable")


class FlakyAttachment(FakeAttachment):
    """Fails the origin fetch and succeeds from the cached proxy copy.

    This is the shape seen live: a just-posted file that 404s once, then
    downloads normally on a second attempt.
    """

    async def save(self, destination: Any, **kwargs: Any) -> None:
        self.attempts += 1
        if not kwargs.get("use_cached"):
            raise OSError("404 while the CDN catches up")
        self.saved_to = Path(destination)
        Path(destination).write_bytes(self.payload)


def _policy(
    *, enabled: bool = True, max_bytes: int = 1024, max_count: int = 5
) -> AttachmentPolicy:
    return AttachmentPolicy(enabled=enabled, max_bytes=max_bytes, max_count=max_count)


async def test_no_attachments_is_not_an_error() -> None:
    result = await collect([], _policy())
    assert result.blocks == []
    assert result.notice == ""


async def test_disabled_policy_sends_nothing_and_says_how_to_enable() -> None:
    result = await collect([FakeAttachment("a.png")], _policy(enabled=False))
    assert result.blocks == []
    assert "/omnigent config" in result.notice


async def test_disabled_policy_never_downloads() -> None:
    # The refusal must come before any network read, not after.
    attachment = FakeAttachment("a.png")
    await collect([attachment], _policy(enabled=False))
    assert attachment.saved_to is None


async def test_image_becomes_an_input_image_block() -> None:
    payload = b"\x89PNG fake"
    result = await collect([FakeAttachment("shot.png", payload)], _policy())
    assert result.skipped == []
    (block,) = result.blocks
    assert block["type"] == "input_image"
    assert block["filename"] == "shot.png"
    encoded = base64.b64encode(payload).decode("ascii")
    assert block["image_url"] == f"data:image/png;base64,{encoded}"


async def test_document_becomes_an_input_file_block() -> None:
    result = await collect([FakeAttachment("notes.md", b"# hi")], _policy())
    (block,) = result.blocks
    assert block["type"] == "input_file"
    # A file block carries its data under file_data, not image_url; the harness
    # reads the two keys differently.
    assert block["file_data"].startswith("data:text/markdown;base64,")
    assert "image_url" not in block


async def test_executable_is_refused_by_name() -> None:
    result = await collect([FakeAttachment("payload.sh", b"#!/bin/sh")], _policy())
    assert result.blocks == []
    assert "looks executable" in result.notice


async def test_executable_is_never_downloaded() -> None:
    attachment = FakeAttachment("payload.exe", b"MZ")
    await collect([attachment], _policy())
    assert attachment.saved_to is None


async def test_unknown_extension_is_refused() -> None:
    result = await collect([FakeAttachment("archive.zip", b"PK")], _policy())
    assert result.blocks == []
    assert "not a supported file type" in result.notice


async def test_oversized_attachment_is_refused_before_download() -> None:
    attachment = FakeAttachment("big.png", b"x" * 50, size=10_000)
    result = await collect([attachment], _policy(max_bytes=1024))
    assert result.blocks == []
    assert attachment.saved_to is None
    assert "over the" in result.notice


async def test_size_is_rechecked_against_the_bytes_actually_read() -> None:
    # Discord's reported size is a claim. A file that under-reports must still
    # be refused once its real length is known.
    attachment = FakeAttachment("liar.png", b"x" * 5000, size=10)
    result = await collect([attachment], _policy(max_bytes=1024))
    assert result.blocks == []
    assert "larger than its reported size" in result.notice


async def test_count_over_the_limit_keeps_the_first_and_reports_the_rest() -> None:
    files = [FakeAttachment(f"f{index}.png") for index in range(4)]
    result = await collect(files, _policy(max_count=2))
    assert [block["filename"] for block in result.blocks] == ["f0.png", "f1.png"]
    assert "2 more attachment(s) ignored" in result.notice


async def test_a_failed_download_skips_only_that_file() -> None:
    files = [ExplodingAttachment("bad.png"), FakeAttachment("good.png", b"ok")]
    result = await collect(files, _policy())
    assert [block["filename"] for block in result.blocks] == ["good.png"]
    assert "could not be downloaded" in result.notice


async def test_insufficient_disk_space_refuses_without_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments_module, "_has_disk_room", lambda *_args: False)
    attachment = FakeAttachment("shot.png", b"data")
    result = await collect([attachment], _policy())
    assert result.blocks == []
    assert attachment.saved_to is None
    assert "free disk space" in result.notice


async def test_disk_check_refuses_when_free_space_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_path: Any) -> None:
        raise OSError("no such device")

    monkeypatch.setattr(attachments_module.shutil, "disk_usage", _raise)
    assert attachments_module._has_disk_room(10, Path("/nowhere")) is False


async def test_disk_check_demands_headroom_beyond_the_raw_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Usage:
        free = 100

    monkeypatch.setattr(attachments_module.shutil, "disk_usage", lambda _p: Usage())
    # Base64 inflates the file, so free space merely equal to its size is not enough.
    assert attachments_module._has_disk_room(100, Path("/tmp")) is False
    assert attachments_module._has_disk_room(10, Path("/tmp")) is True


async def test_a_traversing_filename_cannot_escape_the_scratch_directory() -> None:
    # Discord supplies the name; only its basename may reach the filesystem.
    attachment = FakeAttachment("../../escape.png", b"x")
    result = await collect([attachment], _policy())
    assert attachment.saved_to is not None
    assert attachment.saved_to.name == "escape.png"
    assert ".." not in str(attachment.saved_to)
    # The block still reports the name Discord gave, so the agent sees the truth.
    assert result.blocks[0]["filename"] == "../../escape.png"


async def test_one_refusal_reads_as_a_sentence_and_several_read_as_a_list() -> None:
    single = await collect([FakeAttachment("a.zip")], _policy())
    assert single.notice.startswith("⚠️")
    assert "•" not in single.notice

    several = await collect([FakeAttachment("a.zip"), FakeAttachment("b.exe")], _policy())
    assert several.notice.count("•") == 2


async def test_a_transient_first_fetch_is_retried_from_the_proxy_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments_module, "_RETRY_DELAY_SECONDS", 0)
    attachment = FlakyAttachment("late.png", b"pixels")
    result = await collect([attachment], _policy())
    assert attachment.attempts == 2
    assert result.skipped == []
    assert [block["filename"] for block in result.blocks] == ["late.png"]


async def test_a_download_that_fails_twice_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments_module, "_RETRY_DELAY_SECONDS", 0)
    attachment = ExplodingAttachment("gone.png")
    result = await collect([attachment], _policy())
    assert attachment.attempts == 2
    assert result.blocks == []
    assert "could not be downloaded" in result.notice


async def test_a_healthy_fetch_is_not_retried() -> None:
    attachment = FakeAttachment("fine.png", b"pixels")
    await collect([attachment], _policy())
    assert attachment.attempts == 1
