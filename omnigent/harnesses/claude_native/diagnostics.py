"""Opt-in, bounded forwarding of Claude's native diagnostic file."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from omnigent.debug_logging import debug_event
from omnigent.harnesses.diagnostics import (
    DIAGNOSTIC_TAIL_BYTES,
    bounded_diagnostic_tail,
)
from omnigent.process_logging import harness_stderr_capture_enabled

CLAUDE_DEBUG_LOG_MARKER = "claude-debug-active.json"
_READ_BYTES = 64 * 1024
_MAX_RECORD_BYTES = 1024 * 1024
_CLOSE_READS = 4
_MARKER_BYTES = 4096
_LAUNCH_ID = re.compile(r"[0-9a-f]{32}\Z")
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Capture:
    filename: str
    launch_id: str


def _open_directory(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise OSError("Diagnostic directory is not owner-only")
    return fd


def _open_file(directory_fd: int, filename: str) -> int:
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise OSError("Diagnostic input is not an owned regular file")
    return fd


def _read_capture(directory_fd: int) -> _Capture | None:
    try:
        fd = _open_file(directory_fd, CLAUDE_DEBUG_LOG_MARKER)
    except FileNotFoundError:
        return None
    try:
        raw = os.read(fd, _MARKER_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MARKER_BYTES:
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return None
    launch_id = payload.get("launch_id")
    if not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None:
        return None
    filename = f"claude-debug-{launch_id}.log"
    return _Capture(filename, launch_id) if payload.get("filename") == filename else None


def _clear_capture(directory_fd: int) -> None:
    with contextlib.suppress(OSError, ValueError):
        capture = _read_capture(directory_fd)
        if capture is not None:
            for filename in (capture.filename, capture.filename + ".1"):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(filename, dir_fd=directory_fd)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(CLAUDE_DEBUG_LOG_MARKER, dir_fd=directory_fd)


def augment_claude_debug_args(args: list[str], bridge_dir: Path) -> list[str]:
    """Add a fresh owned debug file when opted in, preserving explicit user flags."""
    if not harness_stderr_capture_enabled():
        return args
    directory_fd: int | None = None
    created: Path | None = None
    try:
        from omnigent.harnesses.claude_native.bridge import _ensure_secure_dir, _write_json_file

        _ensure_secure_dir(bridge_dir)
        directory_fd = _open_directory(bridge_dir)
        _clear_capture(directory_fd)
        separator = args.index("--") if "--" in args else len(args)
        if any(
            arg == "--debug-file" or arg.startswith("--debug-file=") for arg in args[:separator]
        ):
            return args
        launch_id = uuid.uuid4().hex
        filename = f"claude-debug-{launch_id}.log"
        fd = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.close(fd)
        created = bridge_dir / filename
        _write_json_file(
            bridge_dir / CLAUDE_DEBUG_LOG_MARKER,
            {"filename": filename, "launch_id": launch_id},
        )
        return [*args[:separator], "--debug-file", str(created), *args[separator:]]
    except Exception:  # noqa: BLE001 — diagnostics must never prevent a launch
        if created is not None:
            with contextlib.suppress(OSError):
                created.unlink()
        return args
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


class ClaudeDebugLogFollower:
    """Follow only the current owned file, with bounded reads and record buffering."""

    def __init__(self, bridge_dir: Path) -> None:
        self._bridge_dir = bridge_dir
        self._capture: _Capture | None = None
        self._fd: int | None = None
        self._offset = 0
        self._pending = bytearray()
        self._dropping = False
        self._closed = False

    def _reset_file(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._fd = None
        self._offset = 0
        self._pending.clear()
        self._dropping = False

    def _read(self) -> tuple[bytes, list[str]]:
        """Read one chunk, draining a renamed inode before switching to its replacement."""
        directory_fd = _open_directory(self._bridge_dir)
        candidate: int | None = None
        try:
            capture = _read_capture(directory_fd)
            if capture != self._capture:
                self._reset_file()
                self._capture = capture
            if capture is None:
                return b"", []
            with contextlib.suppress(FileNotFoundError):
                candidate = _open_file(directory_fd, capture.filename)
            if self._fd is None:
                self._fd, candidate = candidate, None
            if self._fd is None:
                return b"", []
            info = os.fstat(self._fd)
            if info.st_size < self._offset:
                os.lseek(self._fd, 0, os.SEEK_SET)
                self._offset = 0
                self._pending.clear()
                self._dropping = False
            raw = os.read(self._fd, _READ_BYTES)
            self._offset += len(raw)
            records: list[str] = []
            if not raw and candidate is not None:
                replacement = os.fstat(candidate)
                if (replacement.st_dev, replacement.st_ino) != (info.st_dev, info.st_ino):
                    records = self._finish_record()
                    self._reset_file()
                    self._fd, candidate = candidate, None
                    raw = os.read(self._fd, _READ_BYTES)
                    self._offset = len(raw)
            return raw, records
        finally:
            if candidate is not None:
                os.close(candidate)
            os.close(directory_fd)

    def _feed(self, raw: bytes) -> tuple[list[str], int, int]:
        records: list[str] = []
        omitted_lines = omitted_bytes = 0
        pieces = raw.split(b"\n")
        for index, piece in enumerate(pieces):
            newline = index < len(pieces) - 1
            if self._dropping:
                omitted_bytes += len(piece) + newline
                if newline:
                    self._dropping = False
            elif len(self._pending) + len(piece) > _MAX_RECORD_BYTES:
                omitted_lines += 1
                omitted_bytes += len(self._pending) + len(piece) + newline
                self._pending.clear()
                self._dropping = not newline
            else:
                self._pending.extend(piece)
                if newline:
                    records.append(self._pending.decode("utf-8", errors="replace"))
                    self._pending.clear()
        return records, omitted_lines, omitted_bytes

    def _finish_record(self) -> list[str]:
        records = [self._pending.decode("utf-8", errors="replace")] if self._pending else []
        self._pending.clear()
        self._dropping = False
        return records

    def _remaining_bytes(self, session_id: str, *, prefer_latest: bool = False) -> int:
        """Account for both the open inode and a rotated replacement at shutdown."""
        if self._fd is None or self._capture is None:
            return 0
        info = os.fstat(self._fd)
        remaining = max(0, info.st_size - self._offset)
        directory_fd = _open_directory(self._bridge_dir)
        candidate: int | None = None
        try:
            if _read_capture(directory_fd) != self._capture:
                return remaining
            with contextlib.suppress(FileNotFoundError):
                candidate = _open_file(directory_fd, self._capture.filename)
            if candidate is None:
                return remaining
            replacement = os.fstat(candidate)
            if (replacement.st_dev, replacement.st_ino) == (info.st_dev, info.st_ino):
                return remaining
            if prefer_latest:
                # Spend the bounded final drain on the newest failure evidence.
                self._emit(
                    session_id, [], int(bool(self._pending)), remaining + len(self._pending)
                )
                self._reset_file()
                self._fd, candidate = candidate, None
                return replacement.st_size
            return remaining + replacement.st_size
        finally:
            if candidate is not None:
                os.close(candidate)
            os.close(directory_fd)

    def _emit(
        self,
        session_id: str,
        records: list[str],
        omitted_lines: int = 0,
        omitted_bytes: int = 0,
    ) -> None:
        if self._capture is None or not (records or omitted_lines or omitted_bytes):
            return
        snapshot = bounded_diagnostic_tail(records)
        text = snapshot["tail"]
        total_lines_omitted = cast("int", snapshot["lines_omitted"]) + omitted_lines
        total_bytes_omitted = cast("int", snapshot["bytes_omitted"]) + omitted_bytes
        _logger.info(
            "Claude diagnostic output; session=%s launch=%s offset=%d "
            "lines_omitted=%d bytes_omitted=%d\n%s",
            session_id,
            self._capture.launch_id,
            self._offset,
            total_lines_omitted,
            total_bytes_omitted,
            text,
            extra=debug_event(
                "harness_diagnostic_output",
                session_id=session_id,
                harness="claude-native",
                source_kind="claude_debug_log",
                launch_id=self._capture.launch_id,
                offset=self._offset,
                text=text,
                truncated=bool(snapshot["truncated"] or omitted_lines or omitted_bytes),
                lines_omitted=total_lines_omitted,
                bytes_omitted=total_bytes_omitted,
                tail_byte_limit=DIAGNOSTIC_TAIL_BYTES,
            ),
        )

    def poll(self, session_id: str) -> None:
        """Export newly completed records without blocking on a pipe or unbounded input."""
        if self._closed or not harness_stderr_capture_enabled():
            return
        try:
            raw, previous = self._read()
            records, omitted_lines, omitted_bytes = self._feed(raw)
            self._emit(session_id, [*previous, *records], omitted_lines, omitted_bytes)
        except Exception:  # noqa: BLE001 — diagnostics cannot stop transcript forwarding
            pass

    def close(self, session_id: str) -> None:
        """Drain a bounded final batch and flush a partial record only at the observed EOF."""
        if self._closed:
            return
        try:
            if harness_stderr_capture_enabled():
                self._remaining_bytes(session_id, prefer_latest=True)
                for _ in range(_CLOSE_READS):
                    self.poll(session_id)
                if self._fd is not None:
                    unread = self._remaining_bytes(session_id)
                    if unread:
                        self._emit(
                            session_id, [], int(bool(self._pending)), unread + len(self._pending)
                        )
                    else:
                        self._emit(session_id, self._finish_record())
        except Exception:  # noqa: BLE001 — cleanup must not replace the terminal's outcome
            pass
        finally:
            with contextlib.suppress(OSError):
                self._reset_file()
            self._closed = True
