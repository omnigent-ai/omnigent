"""Session-scoped transcript discovery and JSONL tailing for agy 1.2.x."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from omnigent.harnesses.antigravity_native.bridge import agy_gemini_dir

_logger = logging.getLogger(__name__)
_BASELINE_FILE = "transcript-baseline.json"
_STOP_EVENTS_FILE = "stop-events.jsonl"


@dataclass(frozen=True)
class TranscriptBinding:
    conversation_id: str
    path: Path


def extract_user_request(content: object) -> str | None:
    """Extract only the user-authored text from agy's wrapped USER_INPUT."""
    if not isinstance(content, str):
        return None
    opening = "<USER_REQUEST>"
    closing = "</USER_REQUEST>"
    before, marker, rest = content.partition(opening)
    if marker == "" or before.strip():
        return None
    request, marker, _metadata = rest.rpartition(closing)
    if marker == "":
        return None
    return request.strip("\n")


def _valid_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _safe_file(path: Path, root: Path) -> bool:
    """Reject symlinks and any path outside the bridge-owned Gemini dir."""
    try:
        if (
            root.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            return False
        current = path
        while current != root:
            if current.is_symlink():
                return False
            current = current.parent
    except OSError:
        return False
    return True


def _open_owned_file(path: Path, root: Path) -> int:
    """Open each path component without following a later symlink swap."""
    relative = path.relative_to(root)
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("Transcript path is outside its bridge")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)


def resolve_owned_transcript(bridge_dir: Path) -> TranscriptBinding | None:
    """Use only the single root conversation selected by this isolated CLI."""
    root = agy_gemini_dir(bridge_dir)
    if (bridge_dir / "agy-home").is_symlink() or root.is_symlink():
        return None
    app_dir = root / "antigravity-cli"
    cache = app_dir / "cache" / "last_conversations.json"
    if not _safe_file(cache, root):
        return None
    try:
        mapping = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(mapping, dict):
        return None
    values = list(mapping.values())
    if not values or not all(_valid_id(value) for value in values):
        return None
    ids = set(values)
    if len(ids) != 1:
        return None
    conversation_id = ids.pop()
    path = (
        app_dir
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    if not _safe_file(path, root):
        return None
    return TranscriptBinding(conversation_id, path)


def prepare_transcript_capture(bridge_dir: Path) -> None:
    """Remember existing transcript sizes before a TUI launch or resume."""
    root = agy_gemini_dir(bridge_dir)
    brain = root / "antigravity-cli" / "brain"
    offsets: dict[str, dict[str, int]] = {}
    if brain.is_dir() and not brain.is_symlink():
        for candidate in brain.iterdir():
            if not _valid_id(candidate.name):
                continue
            transcript = candidate / ".system_generated" / "logs" / "transcript_full.jsonl"
            if _safe_file(transcript, root):
                stat = transcript.stat()
                offsets[candidate.name] = {
                    "offset": stat.st_size,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                }
    path = bridge_dir / _BASELINE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(offsets, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    (bridge_dir / _STOP_EVENTS_FILE).unlink(missing_ok=True)


def initial_tail_state(
    bridge_dir: Path, conversation_id: str
) -> tuple[int, tuple[int, int] | None]:
    try:
        data = json.loads((bridge_dir / _BASELINE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, None
    if not isinstance(data, dict):
        return 0, None
    value = data.get(conversation_id)
    if not isinstance(value, dict):
        return 0, None
    offset = value.get("offset")
    device = value.get("device")
    inode = value.get("inode")
    if not all(
        isinstance(part, int) and not isinstance(part, bool) and part >= 0
        for part in (offset, device, inode)
    ):
        return 0, None
    assert isinstance(offset, int) and isinstance(device, int) and isinstance(inode, int)
    return offset, (device, inode)


def transcript_changed_since_launch(bridge_dir: Path, binding: TranscriptBinding) -> bool:
    """Ignore a stale cached conversation until this launch writes a new step."""
    offset, identity = initial_tail_state(bridge_dir, binding.conversation_id)
    try:
        stat = binding.path.stat()
    except OSError:
        return False
    return stat.st_size > offset or (
        identity is not None and (stat.st_dev, stat.st_ino) != identity
    )


def transcript_boundary(bridge_dir: Path, conversation_id: str) -> tuple[int, int, int] | None:
    """Snapshot the last complete byte in this bridge's selected transcript."""
    binding = resolve_owned_transcript(bridge_dir)
    if binding is None or binding.conversation_id != conversation_id:
        return None
    try:
        fd = _open_owned_file(binding.path, agy_gemini_dir(bridge_dir))
        with os.fdopen(fd, "rb") as handle:
            stat = os.fstat(handle.fileno())
            end = stat.st_size
            while end > 0:
                start = max(0, end - 65536)
                handle.seek(start)
                chunk = handle.read(end - start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    return stat.st_dev, stat.st_ino, start + newline + 1
                end = start
            return stat.st_dev, stat.st_ino, 0
    except (OSError, ValueError):
        return None


class JsonlTail:
    """Read complete appended lines, resetting after replacement or truncation."""

    def __init__(
        self,
        path: Path,
        *,
        offset: int = 0,
        identity: tuple[int, int] | None = None,
        safe_root: Path | None = None,
        include_offsets: bool = False,
    ) -> None:
        self.path = path
        self.offset = offset
        self.identity = identity
        self.safe_root = safe_root
        self.include_offsets = include_offsets

    def read(self) -> list[dict[str, object]]:
        try:
            fd = (
                _open_owned_file(self.path, self.safe_root)
                if self.safe_root is not None
                else os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            )
            with os.fdopen(fd, "rb") as handle:
                stat = os.fstat(handle.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if stat.st_size < self.offset or (
                    self.identity is not None and identity != self.identity
                ):
                    self.offset = 0
                self.identity = identity
                handle.seek(self.offset)
                data = handle.read()
        except (OSError, ValueError):
            return []
        last_newline = data.rfind(b"\n")
        if last_newline < 0:
            return []
        first_offset = self.offset
        self.offset += last_newline + 1
        records: list[dict[str, object]] = []
        line_end = first_offset
        for line in data[: last_newline + 1].split(b"\n")[:-1]:
            line_end += len(line) + 1
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                _logger.warning("Skipping malformed agy transcript JSONL record")
                continue
            if isinstance(record, dict):
                if self.include_offsets:
                    record["_transcript_end_offset"] = line_end
                records.append(record)
        return records
