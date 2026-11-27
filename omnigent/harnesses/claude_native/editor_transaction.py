"""Prepare, commit or cancel one exact draft while the CLI waits for its editor."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from omnigent.harnesses.claude_native.editor_owner import NativeDraftOwner

_MAX_DRAFT_BYTES = 1024 * 1024


class NativeDraftEditRefused(RuntimeError):
    """The exported file or its physical native owner no longer matches."""


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class NativeDraftEdit:
    """Retain one verified descriptor until import is released or cancelled.

    A controller must keep the native CLI parked in its editor through these
    phases. Releasing permits import; recovery after import needs a new edit.
    """

    def __init__(
        self,
        path: Path,
        *,
        expected_text: str,
        expected_owner: NativeDraftOwner,
        current_owner: Callable[[], NativeDraftOwner | None],
        cancelled: Callable[[], bool],
    ) -> None:
        self.path = path
        self.owner = expected_owner
        self.current_owner = current_owner
        self.cancelled = cancelled
        self.phase = "unopened"
        self._descriptor: int | None = None
        self._identity: tuple[int, ...] = ()
        if not path.is_absolute():
            raise NativeDraftEditRefused("The exported draft needs an absolute path.")
        self._original = self._encode(expected_text)
        self._current = self._original

    @staticmethod
    def _encode(text: str) -> bytes:
        try:
            encoded = text.encode("utf-8")
        except UnicodeError as exc:
            raise NativeDraftEditRefused("The saved draft is not valid UTF-8.") from exc
        if len(encoded) > _MAX_DRAFT_BYTES:
            raise NativeDraftEditRefused("The saved draft exceeds the editor handoff limit.")
        return encoded

    def _require_owner(self, *, restoring: bool = False) -> None:
        if (not restoring and self.cancelled()) or self.current_owner() != self.owner:
            raise NativeDraftEditRefused(
                "The editor operation was cancelled or its owner changed."
            )

    def _require_file(self, *, restoring: bool = False) -> None:
        self._require_owner(restoring=restoring)
        descriptor = self._descriptor
        if descriptor is None:
            raise NativeDraftEditRefused("The editor operation no longer owns a file.")
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or _identity(info) != self._identity
            or _identity(self.path.lstat()) != self._identity
            or os.pread(descriptor, len(self._current) + 1, 0) != self._current
            or _identity(os.fstat(descriptor)) != self._identity
        ):
            raise NativeDraftEditRefused("The exported draft changed; it was left untouched.")
        self._require_owner(restoring=restoring)

    def __enter__(self) -> NativeDraftEdit:
        if self.phase != "unopened":
            raise NativeDraftEditRefused("An editor operation can only be claimed once.")
        self._require_owner()
        descriptor = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        self._descriptor = descriptor
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._identity = _identity(os.fstat(descriptor))
            self._require_file()
        except BaseException:
            self._close()
            raise
        self.phase = "prepared"
        return self

    def _replace(self, replacement: bytes, *, restoring: bool = False) -> None:
        self._require_file(restoring=restoring)
        descriptor = self._descriptor
        assert descriptor is not None
        offset = 0
        while offset < len(replacement):
            self._require_file(restoring=restoring)
            written = os.pwrite(descriptor, replacement[offset:], offset)
            if written <= 0:
                raise OSError("The editor could not write the exported draft.")
            offset += written
            self._current = replacement[:offset] + self._current[offset:]
            self._record_write(descriptor)
        self._require_file(restoring=restoring)
        os.ftruncate(descriptor, len(replacement))
        self._current = replacement
        self._record_write(descriptor)
        self._require_file(restoring=restoring)

    def _record_write(self, descriptor: int) -> None:
        observed = _identity(os.fstat(descriptor))
        if observed[:6] != self._identity[:6]:
            raise NativeDraftEditRefused("The exported draft's ownership changed.")
        self._identity = observed

    def commit(self, replacement_text: str) -> None:
        """Change the pinned export, retaining cancellation ownership until release."""
        if self.phase != "prepared":
            raise NativeDraftEditRefused("The editor operation is not prepared.")
        replacement = self._encode(replacement_text)
        self.phase = "committing"
        self._replace(replacement)
        self.phase = "committed"

    def release(self) -> None:
        """Permit the native editor to return and import the committed bytes once."""
        if self.phase != "committed":
            raise NativeDraftEditRefused("The editor operation has no committed draft.")
        self._require_file()
        self.phase = "released"

    def cancel(self) -> None:
        """Restore only the exact file state this operation still owns."""
        if self.phase in {"released", "cancelled", "closed", "unopened"}:
            return
        self._require_file(restoring=True)
        if self._current != self._original:
            self._replace(self._original, restoring=True)
        self.phase = "cancelled"

    def _close(self) -> None:
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            os.close(descriptor)

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            cancelled_release = False
            if self.phase == "released":
                self._require_file(restoring=True)
                cancelled_release = self.cancelled()
                if exception is not None or cancelled_release:
                    self.phase = "committed"
            self.cancel()
            if cancelled_release and exception is None:
                raise NativeDraftEditRefused("The editor operation was cancelled before import.")
        finally:
            self._close()
            if self.phase != "released":
                self.phase = "closed"
