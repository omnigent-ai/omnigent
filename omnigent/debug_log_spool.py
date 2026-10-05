"""Local spool for debug-log rows the sink could not deliver.

Rows land here at shutdown (instead of waiting on the network) and after a
definite delivery failure. A later process replays them in the background.
Delivery is at-most-once: one process per machine replays (an exclusive file
lock), and a file is renamed to ``*.sending`` before its POST, so a file left in
that state by a dead uploader is dropped rather than sent again.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

if sys.platform == "win32":
    import msvcrt

    def _lock_nonblocking(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_nonblocking(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_logger = logging.getLogger(__name__)

SPOOL_DIR_NAME = "debug-log-spool"
_LOCK_NAME = "upload.lock"
_READY_SUFFIX = ".jsonl"
_SENDING_SUFFIX = ".sending"
_TMP_SUFFIX = ".tmp"
_FORMAT_VERSION = 1

ROWS_PER_FILE = 100
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILES = 1000
MAX_AGE_S = 7 * 24 * 3600.0
MAX_ROW_BYTES = 1024 * 1024

DebugLogRow = dict[str, object]
# ``failed``: definitely not accepted, safe to keep for a retry. ``unknown``: the
# request may have been accepted (e.g. a read timeout), so it must not be resent.
# ``rejected``: the endpoint permanently refused it (e.g. a 400).
DeliveryResult = Literal["delivered", "failed", "unknown", "rejected"]
ReplayResult = Literal["done", "paused", "failed", "busy"]
Diag = Callable[..., None]

# Lock fds held by this process. A forked child closes its copies so it never
# pins the parent's lock; closing (not unlocking) leaves the parent's lock held.
_held_lock_fds: set[int] = set()


def _close_inherited_lock_fds() -> None:
    for fd in list(_held_lock_fds):
        with contextlib.suppress(OSError):
            os.close(fd)
    _held_lock_fds.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_lock_fds)


def _default_diag(key: str, msg: str, *args: object) -> None:  # noqa: ARG001
    _logger.warning("debug-log spool: " + msg, *args)


def destination_key(destination: str) -> str:
    """Return the stable, non-secret key that pins a spool file to its endpoint."""
    return hashlib.sha256(destination.encode("utf-8")).hexdigest()[:16]


class DebugLogSpool:
    """A directory of spooled debug-log batches for one delivery destination."""

    def __init__(self, directory: Path, destination: str, *, diag: Diag | None = None) -> None:
        self._dir = directory
        self._dest = destination_key(destination)
        self._diag = diag or _default_diag
        self._seq = itertools.count()

    @classmethod
    def for_destination(cls, destination: str, *, diag: Diag | None = None) -> DebugLogSpool:
        """Spool under ``<data-dir>/debug-log-spool`` for *destination*."""
        from omnigent.process_logging import data_dir

        return cls(data_dir() / SPOOL_DIR_NAME, destination, diag=diag)

    @property
    def directory(self) -> Path:
        return self._dir

    # ── writing ─────────────────────────────────────────────────────────────

    def write(self, rows: Iterable[DebugLogRow], *, deadline: float | None = None) -> int:
        """Persist *rows* as spool files of at most :data:`ROWS_PER_FILE` rows.

        Local disk only; never touches the network. Stops early once the
        monotonic *deadline* passes.

        :returns: Number of rows written.
        """
        encoded: list[str] = []
        oversize = 0
        for row in rows:
            try:
                line = json.dumps(row, default=str)
            except (TypeError, ValueError):
                oversize += 1
                continue
            if len(line) > MAX_ROW_BYTES:
                oversize += 1
                continue
            encoded.append(line)
        if oversize:
            self._diag("spool_oversize", "dropped %d unserializable/oversize row(s)", oversize)
        if not encoded:
            return 0
        chunks = [encoded[i : i + ROWS_PER_FILE] for i in range(0, len(encoded), ROWS_PER_FILE)]
        written = 0
        try:
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._prune(
                incoming_files=len(chunks),
                incoming_bytes=sum(len(line) + 1 for line in encoded),
            )
            for chunk in chunks:
                if deadline is not None and time.monotonic() > deadline:
                    break
                self._write_file(chunk)
                written += len(chunk)
        except OSError as exc:
            self._diag("spool_write", "could not write spool file: %s", exc)
        if written < len(encoded):
            self._diag(
                "spool_incomplete", "dropped %d row(s) not spooled in time", len(encoded) - written
            )
        return written

    def _write_file(self, lines: list[str]) -> None:
        name = f"{int(time.time() * 1000):013d}-{os.getpid()}-{next(self._seq)}"
        final = self._dir / f"{name}{_READY_SUFFIX}"
        tmp = self._dir / f"{name}{_TMP_SUFFIX}"
        header = json.dumps({"v": _FORMAT_VERSION, "dest": self._dest, "rows": len(lines)})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(header + "\n")
                fh.write("\n".join(lines) + "\n")
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        # Atomic: a replayer never sees a partial file.
        os.replace(tmp, final)

    def _prune(self, *, incoming_files: int, incoming_bytes: int) -> None:
        """Delete expired files, then the oldest ones, to fit the size caps."""
        entries: list[tuple[Path, int]] = []
        now_ms = time.time() * 1000
        for path in sorted(self._dir.glob(f"*{_READY_SUFFIX}")):
            if now_ms - _created_ms(path) > MAX_AGE_S * 1000:
                self._drop_file(path, "spool_expired", "dropped %d expired spooled row(s)")
                continue
            with contextlib.suppress(OSError):
                entries.append((path, path.stat().st_size))
        total = sum(size for _, size in entries)
        while entries and (
            len(entries) + incoming_files > MAX_FILES or total + incoming_bytes > MAX_TOTAL_BYTES
        ):
            path, size = entries.pop(0)
            self._drop_file(path, "spool_cap", "spool full; dropped %d oldest row(s)")
            total -= size

    # ── replay ──────────────────────────────────────────────────────────────

    def replay(
        self,
        deliver: Callable[[list[DebugLogRow]], DeliveryResult],
        *,
        should_continue: Callable[[], bool],
    ) -> ReplayResult:
        """Deliver spooled files for this destination, oldest first.

        :param deliver: Sends one batch and reports its outcome.
        :param should_continue: Checked between files; ``False`` pauses
            (e.g. live rows are waiting, or the sink is closing).
        :returns: ``"done"`` when nothing is left, ``"paused"`` when stopped
            early, ``"failed"`` on a retryable failure (the file is kept), or
            ``"busy"`` when another process holds the upload lock.
        """
        if not self._dir.is_dir():
            return "done"
        lock_fd = self._try_lock()
        if lock_fd is None:
            return "busy"
        try:
            self._drop_unknown_outcomes()
            now_ms = time.time() * 1000
            for path in sorted(self._dir.glob(f"*{_READY_SUFFIX}")):
                if not should_continue():
                    return "paused"
                if now_ms - _created_ms(path) > MAX_AGE_S * 1000:
                    self._drop_file(path, "spool_expired", "dropped %d expired spooled row(s)")
                    continue
                parsed = self._read(path)
                if parsed is None:
                    continue
                dest, rows = parsed
                if dest != self._dest:
                    continue  # another endpoint's file; left for its own sink
                sending = path.with_suffix(_SENDING_SUFFIX)
                try:
                    os.replace(path, sending)
                except OSError:
                    continue
                result = deliver(_tag_replayed(rows))
                if result == "failed":
                    with contextlib.suppress(OSError):
                        os.replace(sending, path)
                    return "failed"
                if result == "unknown":
                    self._diag(
                        "spool_unknown", "dropped %d replayed row(s): outcome unknown", len(rows)
                    )
                elif result == "rejected":
                    self._diag("spool_rejected", "dropped %d replayed row(s): rejected", len(rows))
                with contextlib.suppress(OSError):
                    sending.unlink()
            return "done"
        finally:
            self._unlock(lock_fd)

    def _drop_unknown_outcomes(self) -> None:
        """Drop files a dead uploader left mid-POST; resending could duplicate."""
        for path in self._dir.glob(f"*{_SENDING_SUFFIX}"):
            self._drop_file(
                path, "spool_unknown", "dropped %d spooled row(s): previous upload outcome unknown"
            )
        # Temp files are only ever live inside a writer's _write_file; anything
        # older than a minute is debris from a writer that died mid-write.
        cutoff = time.time() - 60
        for path in self._dir.glob(f"*{_TMP_SUFFIX}"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    def _read(self, path: Path) -> tuple[str, list[DebugLogRow]] | None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            header = json.loads(lines[0])
            rows = [json.loads(line) for line in lines[1:] if line]
            if not isinstance(header, dict) or header.get("v") != _FORMAT_VERSION:
                raise ValueError("unsupported spool header")
            dest = header.get("dest")
            if not isinstance(dest, str) or not all(isinstance(r, dict) for r in rows):
                raise ValueError("malformed spool file")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError) as exc:
            self._diag("spool_corrupt", "dropping unreadable spool file %s: %s", path.name, exc)
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        return dest, rows

    def _drop_file(self, path: Path, key: str, msg: str) -> None:
        count = _header_row_count(path)
        with contextlib.suppress(OSError):
            path.unlink()
            self._diag(key, msg, count)

    # ── locking ─────────────────────────────────────────────────────────────

    def _try_lock(self) -> int | None:
        try:
            fd = os.open(self._dir / _LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            _lock_nonblocking(fd)
        except OSError:
            os.close(fd)
            return None
        _held_lock_fds.add(fd)
        return fd

    def _unlock(self, fd: int) -> None:
        _held_lock_fds.discard(fd)
        with contextlib.suppress(OSError):
            _unlock_fd(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


def _created_ms(path: Path) -> float:
    try:
        return float(path.name.split("-", 1)[0])
    except ValueError:
        return 0.0


def _header_row_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as fh:
            header = json.loads(fh.readline())
        rows = header.get("rows") if isinstance(header, dict) else None
        return rows if isinstance(rows, int) else 0
    except (OSError, ValueError):
        return 0


def _tag_replayed(rows: list[DebugLogRow]) -> list[DebugLogRow]:
    """Mark replayed rows so late arrivals are distinguishable in queries."""
    now_us = time.time() * 1_000_000
    tagged: list[DebugLogRow] = []
    for row in rows:
        attrs = row.get("attributes")
        merged = dict(attrs) if isinstance(attrs, dict) else {}
        merged["spooled"] = "true"
        client_time = row.get("client_time")
        if isinstance(client_time, (int, float)):
            merged["spool_delay_s"] = f"{max(0.0, (now_us - client_time) / 1_000_000):.3f}"
        tagged.append({**row, "attributes": merged})
    return tagged
