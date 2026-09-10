"""Turn Discord message attachments into Omnigent content blocks.

Omnigent carries a file to the agent as a content block holding a base64 data
URI, which the host materializes to disk for the harness to read:

    {"type": "input_image", "image_url": "data:image/png;base64,...", ...}
    {"type": "input_file", "file_data": "data:application/pdf;base64,...", ...}

Fetching a file that an arbitrary Discord user attached is the one place this
bot pulls unbounded bytes from the network, so every limit here is a refusal
rather than a truncation: a file that is too large, of an unknown type, or that
would not fit on disk is skipped and reported, never partially sent.

Uploads are off unless BOTH the operator (``OMNIGENT_DISCORD_ALLOW_FILE_UPLOAD``)
and the user (``/omnigent config``) have turned them on.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Extensions we will forward, grouped by the block type Omnigent expects.
# An ALLOW-list, not a deny-list: a new dangerous extension appearing in the
# wild must not silently become forwardable, and the harness only does useful
# work with these anyway.
_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_FILE_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".log": "text/plain",
    ".py": "text/x-python",
    ".ts": "text/plain",
    ".js": "text/plain",
    ".go": "text/plain",
    ".rs": "text/plain",
    ".sql": "text/plain",
    ".html": "text/html",
    ".css": "text/css",
    ".xml": "application/xml",
}

# Named only to explain the refusal. Anything outside the allow-list above is
# refused regardless; these get a message that says why rather than the generic
# "unsupported type", because a user attaching one is usually trying to get the
# agent to run it.
_EXECUTABLE_EXTENSIONS = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".app",
        ".msi",
        ".apk",
        ".jar",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".com",
        ".scr",
        ".deb",
        ".rpm",
        ".dmg",
        ".pkg",
        ".run",
        ".elf",
    }
)

# Leave room for the file itself plus its base64 form (4/3 the size) plus slack
# for whatever else shares the disk. Refusing early beats a partial write and an
# ENOSPC halfway through someone's turn.
_DISK_HEADROOM_FACTOR = 3

# Long enough for the CDN to settle, short enough that the turn does not stall.
_RETRY_DELAY_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class AttachmentPolicy:
    """The limits a single message's attachments are judged against."""

    enabled: bool
    max_bytes: int
    max_count: int

    @property
    def max_megabytes(self) -> float:
        return self.max_bytes / (1024 * 1024)


@dataclass(frozen=True, slots=True)
class AttachmentResult:
    """Blocks to send, and a human-readable note for anything refused."""

    blocks: list[dict[str, Any]]
    skipped: list[str]

    @property
    def notice(self) -> str:
        """A line to show the user, or empty when nothing was refused."""
        if not self.skipped:
            return ""
        if len(self.skipped) == 1:
            return f"⚠️ {self.skipped[0]}"
        joined = "\n".join(f"• {reason}" for reason in self.skipped)
        return f"⚠️ Some attachments were not sent:\n{joined}"


def _classify(filename: str) -> tuple[str, str] | None:
    """Map a filename to its ``(block_type, mime)``, or ``None`` if refused."""
    suffix = Path(filename).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "input_image", _IMAGE_EXTENSIONS[suffix]
    if suffix in _FILE_EXTENSIONS:
        return "input_file", _FILE_EXTENSIONS[suffix]
    return None


def _refusal_reason(filename: str, policy: AttachmentPolicy, size: int) -> str | None:
    """Why this attachment cannot be sent, or ``None`` if it can."""
    suffix = Path(filename).suffix.lower()
    if suffix in _EXECUTABLE_EXTENSIONS:
        return f"`{filename}` looks executable, so it was not sent."
    if _classify(filename) is None:
        return f"`{filename}` is not a supported file type, so it was not sent."
    if size > policy.max_bytes:
        return (
            f"`{filename}` is {size / (1024 * 1024):.1f} MB, over the "
            f"{policy.max_megabytes:.0f} MB limit, so it was not sent."
        )
    return None


def _has_disk_room(size: int, directory: Path) -> bool:
    """Whether *directory* can hold *size* bytes with room to encode it."""
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        # An unreadable temp dir is not a reason to attempt the download.
        _logger.warning("Could not read free space for %s; refusing attachment", directory)
        return False
    return free > size * _DISK_HEADROOM_FACTOR


def _build_block(block_type: str, mime: str, filename: str, raw: bytes) -> dict[str, Any]:
    """Wrap file bytes in the content block shape Omnigent materializes."""
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    key = "image_url" if block_type == "input_image" else "file_data"
    return {"type": block_type, key: data_uri, "filename": filename}


async def collect(attachments: list[Any], policy: AttachmentPolicy) -> AttachmentResult:
    """Fetch what is allowed from *attachments* and describe what was not.

    Spools each file to a temporary path rather than holding it in memory, so
    free disk is checked first and the bot's footprint stays flat regardless of
    what someone attaches.
    """
    if not attachments:
        return AttachmentResult(blocks=[], skipped=[])
    if not policy.enabled:
        count = len(attachments)
        noun = "attachment" if count == 1 else "attachments"
        return AttachmentResult(
            blocks=[],
            skipped=[
                f"{count} {noun} ignored: file uploads are off. "
                "Turn them on with `/omnigent config`."
            ],
        )

    blocks: list[dict[str, Any]] = []
    skipped: list[str] = []
    accepted = attachments[: policy.max_count]
    if len(attachments) > policy.max_count:
        dropped = len(attachments) - policy.max_count
        skipped.append(
            f"{dropped} more attachment(s) ignored: at most {policy.max_count} per message."
        )

    temp_root = Path(tempfile.gettempdir())
    for attachment in accepted:
        filename = str(getattr(attachment, "filename", "") or "file")
        size = int(getattr(attachment, "size", 0) or 0)

        reason = _refusal_reason(filename, policy, size)
        if reason is not None:
            skipped.append(reason)
            continue
        if not _has_disk_room(size, temp_root):
            skipped.append(f"`{filename}` was not sent: not enough free disk space.")
            continue

        classified = _classify(filename)
        if classified is None:  # pragma: no cover - _refusal_reason already caught it
            continue
        block_type, mime = classified
        try:
            raw = await _read(attachment, filename, temp_root)
        except Exception as exc:
            _logger.warning(
                "Could not download attachment %s after a retry: %s: %s",
                filename,
                type(exc).__name__,
                exc,
            )
            skipped.append(f"`{filename}` could not be downloaded, so it was not sent.")
            continue

        # Discord's reported size is a hint, not a guarantee; the bytes we
        # actually hold are what must satisfy the limit.
        if len(raw) > policy.max_bytes:
            skipped.append(
                f"`{filename}` is larger than its reported size and over the "
                f"{policy.max_megabytes:.0f} MB limit, so it was not sent."
            )
            continue
        blocks.append(_build_block(block_type, mime, filename, raw))
        _logger.info("Attached %s (%s, %d bytes)", filename, block_type, len(raw))

    return AttachmentResult(blocks=blocks, skipped=skipped)


async def _read(attachment: Any, filename: str, temp_root: Path) -> bytes:
    """Spool one attachment to a temp file and return its bytes.

    Retries once against Discord's cached proxy copy. A first fetch of a
    just-posted attachment can fail while the CDN is still catching up, and
    losing someone's file to a single transient miss is worse than the wait.
    """
    with tempfile.TemporaryDirectory(dir=temp_root) as scratch:
        # Discord supplies the name, so keep only its basename: a crafted
        # "../../x" must not escape the scratch directory.
        target = Path(scratch) / Path(filename).name
        try:
            await attachment.save(target)
        except Exception as first_error:
            _logger.info(
                "Retrying attachment %s via the proxy copy after %s: %s",
                filename,
                type(first_error).__name__,
                first_error,
            )
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
            # ``use_cached`` reads Discord's proxy_url rather than the origin
            # URL, which is what serves a file the origin has not settled yet.
            await attachment.save(target, use_cached=True)
        return target.read_bytes()
