"""Opt-in, bounded forwarding of Claude's native diagnostic file."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

if sys.platform == "win32":
    import _winapi
    import msvcrt

from omnigent.debug_logging import debug_event
from omnigent.harnesses.diagnostics import (
    DIAGNOSTIC_TAIL_BYTES,
    bounded_diagnostic_tail,
)
from omnigent.native.failure_telemetry import FailureContext, normalize_failure_context
from omnigent.process_logging import harness_stderr_capture_enabled

CLAUDE_DEBUG_LOG_MARKER = "claude-debug-active.json"
_READ_BYTES = 64 * 1024
_MAX_RECORD_BYTES = 1024 * 1024
_CLOSE_READS = 4
_MARKER_BYTES = 4096
_LAUNCH_ID = re.compile(r"[0-9a-f]{32}\Z")
_logger = logging.getLogger(__name__)
# Windows has no ``dir_fd`` or ``O_NOFOLLOW``; there the bridge directory is
# used by path and each entry is checked with ``lstat`` before it is opened.
_DIR_FD_SUPPORTED = (
    os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
)
_FILE_SHARE_READ_WRITE_DELETE = 0x7


@dataclass(frozen=True)
class _Capture:
    filename: str
    launch_id: str


def _open_directory(path: Path) -> int | Path:
    if not _DIR_FD_SUPPORTED:
        info = os.lstat(path)
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("Diagnostic directory is a link or not a directory")
        return path
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise OSError("Diagnostic directory is not owner-only")
    return fd


def _close_directory(directory: int | Path) -> None:
    if isinstance(directory, int):
        os.close(directory)


def _entry(directory: int | Path, filename: str) -> tuple[str, int | None]:
    """Name *filename* relative to the directory descriptor, or by path without one."""
    if isinstance(directory, int):
        return filename, directory
    return str(directory / filename), None


def _is_link(info: os.stat_result) -> bool:
    """Whether an ``lstat`` result is a symlink, junction or other reparse point."""
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return stat.S_ISLNK(info.st_mode) or bool(reparse)


if sys.platform == "win32":

    def _open_by_path(path: Path) -> int:
        """Open for reading and still let Claude rotate the file by renaming it."""
        handle = _winapi.CreateFile(
            str(path),
            _winapi.GENERIC_READ,
            _FILE_SHARE_READ_WRITE_DELETE,
            0,
            _winapi.OPEN_EXISTING,
            0,
            0,
        )
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise

else:

    def _open_by_path(path: Path) -> int:
        return os.open(path, os.O_RDONLY)


def _open_file(directory: int | Path, filename: str) -> int:
    if isinstance(directory, int):
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        owned = os.fstat(fd).st_uid == os.getuid()
    else:
        # The opened file must be the entry that lstat checked, not a link swapped in.
        entry = os.lstat(directory / filename)
        fd = _open_by_path(directory / filename)
        owned = not _is_link(entry) and os.path.samestat(entry, os.fstat(fd))
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not owned or info.st_nlink != 1:
        os.close(fd)
        raise OSError("Diagnostic input is not an owned regular file")
    return fd


def _read_capture(directory: int | Path) -> _Capture | None:
    try:
        fd = _open_file(directory, CLAUDE_DEBUG_LOG_MARKER)
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


def _clear_capture(directory: int | Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        capture = _read_capture(directory)
        if capture is not None:
            for filename in (capture.filename, capture.filename + ".1"):
                with contextlib.suppress(FileNotFoundError):
                    name, dir_fd = _entry(directory, filename)
                    os.unlink(name, dir_fd=dir_fd)
    with contextlib.suppress(FileNotFoundError):
        name, dir_fd = _entry(directory, CLAUDE_DEBUG_LOG_MARKER)
        os.unlink(name, dir_fd=dir_fd)


def augment_claude_debug_args(args: list[str], bridge_dir: Path) -> list[str]:
    """Add a fresh owned debug file when opted in, preserving explicit user flags."""
    if not harness_stderr_capture_enabled():
        return args
    directory: int | Path | None = None
    created: Path | None = None
    try:
        from omnigent.harnesses.claude_native.bridge import _ensure_secure_dir, _write_json_file

        _ensure_secure_dir(bridge_dir)
        directory = _open_directory(bridge_dir)
        _clear_capture(directory)
        separator = args.index("--") if "--" in args else len(args)
        if any(
            arg == "--debug-file" or arg.startswith("--debug-file=") for arg in args[:separator]
        ):
            return args
        launch_id = uuid.uuid4().hex
        filename = f"claude-debug-{launch_id}.log"
        name, dir_fd = _entry(directory, filename)
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=dir_fd,
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
        if directory is not None:
            _close_directory(directory)


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
        self._marker_present: bool | None = None
        self._file_present: bool | None = None
        self._last_read_at: float | None = None
        self._last_read_offset: int | None = None
        self._lines_omitted = 0
        self._bytes_omitted = 0
        self._truncated = False
        self._health: FailureContext = {
            "diagnostic_capture_enabled": True,
            "diagnostic_capture_state": "not_polled",
        }

    def health_snapshot(self) -> FailureContext:
        """Return the last completed poll's metadata without reading files or taking its lock."""
        if not harness_stderr_capture_enabled():
            return {"diagnostic_capture_enabled": False, "diagnostic_capture_state": "disabled"}
        return dict(self._health)

    def _update_health(self, error: Exception | None = None) -> None:
        state = (
            "read_error"
            if error is not None
            else "missing_marker"
            if not self._marker_present
            else "invalid_marker"
            if self._capture is None
            else "missing_file"
            if not self._file_present
            else "ready"
        )
        self._health = normalize_failure_context(
            {
                "diagnostic_capture_enabled": True,
                "diagnostic_capture_state": state,
                "diagnostic_marker_present": self._marker_present,
                "diagnostic_file_present": self._file_present,
                "diagnostic_last_read_at": self._last_read_at,
                "diagnostic_read_offset": self._last_read_offset,
                "diagnostic_read_error_kind": type(error).__name__ if error is not None else None,
                "diagnostic_lines_omitted": self._lines_omitted,
                "diagnostic_bytes_omitted": self._bytes_omitted,
                "diagnostic_truncated": self._truncated,
                "diagnostic_launch_id": self._capture.launch_id if self._capture else None,
            }
        )

    def _reset_file(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._fd = None
        self._offset = 0
        self._pending.clear()
        self._dropping = False

    def _read(self) -> tuple[bytes, list[str], int]:
        """Read one chunk, draining a renamed inode before switching to its replacement."""
        directory = _open_directory(self._bridge_dir)
        candidate: int | None = None
        omitted_bytes = 0
        try:
            try:
                name, dir_fd = _entry(directory, CLAUDE_DEBUG_LOG_MARKER)
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                self._marker_present = False
            else:
                self._marker_present = True
            capture = _read_capture(directory)
            if capture != self._capture:
                self._reset_file()
                self._capture = capture
                self._last_read_at = None
                self._last_read_offset = None
                self._lines_omitted = self._bytes_omitted = 0
                self._truncated = False
                # Count the known predecessor only on attachment to this launch;
                # later rotations are handled through the already-open inode.
                if capture is not None:
                    with contextlib.suppress(OSError):
                        predecessor = _open_file(directory, capture.filename + ".1")
                        try:
                            omitted_bytes = os.fstat(predecessor).st_size
                        finally:
                            os.close(predecessor)
            if capture is None:
                return b"", [], omitted_bytes
            with contextlib.suppress(FileNotFoundError):
                candidate = _open_file(directory, capture.filename)
            self._file_present = candidate is not None
            if self._fd is None:
                self._fd, candidate = candidate, None
            if self._fd is None:
                return b"", [], omitted_bytes
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
            self._last_read_at = time.time()
            self._last_read_offset = self._offset
            return raw, records, omitted_bytes
        finally:
            if candidate is not None:
                os.close(candidate)
            _close_directory(directory)

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
        directory = _open_directory(self._bridge_dir)
        candidate: int | None = None
        try:
            if _read_capture(directory) != self._capture:
                return remaining
            with contextlib.suppress(FileNotFoundError):
                candidate = _open_file(directory, self._capture.filename)
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
            _close_directory(directory)

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
        self._lines_omitted += total_lines_omitted
        self._bytes_omitted += total_bytes_omitted
        self._truncated |= bool(snapshot["truncated"] or omitted_lines or omitted_bytes)
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
        self._marker_present = self._file_present = None
        try:
            raw, previous, predecessor_bytes = self._read()
            records, omitted_lines, omitted_bytes = self._feed(raw)
            self._emit(
                session_id, [*previous, *records], omitted_lines, omitted_bytes + predecessor_bytes
            )
        except Exception as exc:  # noqa: BLE001 — diagnostics cannot stop transcript forwarding
            self._update_health(exc)
        else:
            self._update_health()

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
                if self._health.get("diagnostic_capture_state") != "read_error":
                    self._update_health()
        except Exception as exc:  # noqa: BLE001 — cleanup must not replace the terminal's outcome
            self._update_health(exc)
        finally:
            with contextlib.suppress(OSError):
                self._reset_file()
            self._closed = True
