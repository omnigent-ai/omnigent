"""Kernel-held resource leases for a terminal command and its consumer process."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

_MANIFEST = "process-lifetime.json"


@dataclass(frozen=True)
class ProcessLifetimeLease:
    """Immutable file identities supplied by an admitted resource owner."""

    path: Path
    identity: tuple[int, int]
    claim: Path
    claim_identity: tuple[int, int]
    ready: Path | None = None


def encode_process_leases(leases: Sequence[ProcessLifetimeLease]) -> str | None:
    if not leases:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(
            [
                [
                    str(lease.path),
                    list(lease.identity),
                    str(lease.claim),
                    list(lease.claim_identity),
                    str(lease.ready) if lease.ready is not None else None,
                ]
                for lease in leases
            ],
            separators=(",", ":"),
        ).encode()
    ).decode("ascii")


def _decode(encoded: str) -> list[ProcessLifetimeLease]:
    if len(encoded) > 65536:
        raise ValueError("Process resource lease payload is too large")
    payload = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
    if not isinstance(payload, list) or not 1 <= len(payload) <= 16:
        raise ValueError("Invalid process resource leases")
    leases = []
    for entry in payload:
        if (
            not isinstance(entry, list)
            or len(entry) != 5
            or any(not isinstance(entry[index], str) for index in (0, 2))
            or any(
                not isinstance(entry[index], list)
                or len(entry[index]) != 2
                or any(type(value) is not int or value < 0 for value in entry[index])
                or entry[index][1] == 0
                for index in (1, 3)
            )
        ):
            raise ValueError("Invalid process resource lease identity")
        path, claim = Path(entry[0]), Path(entry[2])
        if not path.is_absolute() or not claim.is_absolute() or path.parent != claim.parent:
            raise ValueError("Invalid process resource lease paths")
        ready = Path(entry[4]) if isinstance(entry[4], str) else None
        if (entry[4] is not None and ready is None) or (
            ready is not None and not ready.is_absolute()
        ):
            raise ValueError("Invalid process resource handoff path")
        leases.append(ProcessLifetimeLease(path, tuple(entry[1]), claim, tuple(entry[3]), ready))
    return leases


def process_lease_ready(lease: ProcessLifetimeLease) -> bool:
    if lease.ready is None:
        return False
    try:
        fd = os.open(lease.ready, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            return stat.S_ISREG(os.fstat(stream.fileno()).st_mode) and stream.read(128) == (
                f"{lease.claim_identity[0]}:{lease.claim_identity[1]}".encode("ascii")
            )
    except OSError:
        return False


def write_process_lease_manifest(directory: Path, encoded: str) -> None:
    info = directory.stat(follow_symlinks=False)
    payload = {"schema": 1, "directory_identity": [info.st_dev, info.st_ino], "leases": encoded}
    fd, temporary = tempfile.mkstemp(prefix=".process-lifetime-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            json.dump(payload, stream, sort_keys=True)
        os.replace(temporary, directory / _MANIFEST)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _unused_manifest_leases(directory: Path, descriptors: list[int]) -> bool:
    try:
        fd = os.open(directory / _MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Invalid process lease manifest")
        payload = json.loads(stream.read(131073))
        info = directory.stat(follow_symlinks=False)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "directory_identity", "leases"}
            or type(payload["schema"]) is not int
            or payload["schema"] != 1
            or not isinstance(payload["directory_identity"], list)
            or len(payload["directory_identity"]) != 2
            or any(type(value) is not int for value in payload["directory_identity"])
            or payload["directory_identity"] != [info.st_dev, info.st_ino]
            or not isinstance(payload["leases"], str)
        ):
            raise ValueError("Invalid process lease manifest binding")
        leases = _decode(payload["leases"])
    import fcntl

    for lease in leases:
        try:
            fd = os.open(lease.path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != lease.identity:
            raise ValueError("Process resource lease was replaced")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


@contextlib.contextmanager
def finish_process_leases(directory: Path) -> Iterator[bool | None]:
    """Fence late joins while deleting terminal files; preserve unknown manifests."""
    descriptors: list[int] = []
    try:
        try:
            unused = _unused_manifest_leases(directory, descriptors)
        except (OSError, ValueError, UnicodeError, RecursionError):
            unused = None
        yield unused
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@contextlib.contextmanager
def hold_process_leases(encoded: str | None) -> Iterator[tuple[int, ...]]:
    """Join each exact inode before the target can read any resource operand."""
    if encoded is None:
        yield ()
        return
    import fcntl

    descriptors: list[int] = []
    try:
        leases = _decode(encoded)
        for lease in leases:
            for path, identity in (
                (lease.path, lease.identity),
                (lease.claim, lease.claim_identity),
            ):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                descriptors.append(fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                    raise ValueError("Process resource lease was replaced")
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != identity:
                    raise ValueError("Process resource lease was retired")
        for lease in leases:
            if lease.ready is not None:
                identity = f"{lease.claim_identity[0]}:{lease.claim_identity[1]}".encode("ascii")
                fd = os.open(
                    lease.ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(identity)
        yield tuple(descriptors)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def run_guard(encoded: str, command: Sequence[str]) -> int:
    """Retain the lease through target completion and inherit it into that target."""
    with hold_process_leases(encoded) as descriptors:
        process = subprocess.Popen(command, pass_fds=descriptors)
        previous = {}
        try:
            # Interactive interrupts already reach the child's foreground process group.
            previous[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)
            for signum in (signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(
                    signum, lambda received, _frame: process.send_signal(received)
                )
            result = process.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return result if result >= 0 else 128 - result


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        raise SystemExit("Invalid terminal resource guard invocation")
    raise SystemExit(run_guard(sys.argv[1], sys.argv[3:]))
