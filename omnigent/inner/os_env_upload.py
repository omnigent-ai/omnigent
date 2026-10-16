"""Bounded raw upload RPC over the existing sandboxed helper launcher."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import re
import stat
import struct
import sys
import threading
import time
from collections.abc import Buffer
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast

from omnigent.inner.os_env import (
    CallerProcessOSEnvironment,
    _assert_within_reach,
    _assert_write_allowed,
    _HelperProcessClient,
)
from omnigent.inner.sandbox import SandboxPolicy

MAX_RENAME_ATTEMPTS = 1000
CALL_TIMEOUT_S = 30
SHORT_OP_QUEUE_WAIT_S = 1
LONG_OPS = ("plan", "complete", "recover")
COMPLETE_TIMEOUT_FLOOR_S = 120
COMPLETE_TIMEOUT_PER_GIB_S = 300


def operation_timeout(metadata: dict[str, object]) -> float:
    if metadata.get("op") in LONG_OPS:
        size = metadata.get("size", 0)
        return COMPLETE_TIMEOUT_FLOOR_S + COMPLETE_TIMEOUT_PER_GIB_S * (
            max(0, size) / 1024**3 if isinstance(size, int) else 0
        )
    return CALL_TIMEOUT_S


MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
STAGING_SUFFIX_PATTERN = r"\.omnigent-upload-[0-9a-f]{32}\.part\Z"


def is_upload_staging(path: str) -> bool:
    return re.search(STAGING_SUFFIX_PATTERN, path.rsplit("/", 1)[-1]) is not None


def staging_basename(name: str, uid: str) -> str:
    suffix = f".omnigent-upload-{uid}.part"
    # Existing dotfiles are masked when a fresh sandboxed helper starts.
    prefix = name.lstrip(".") or "upload"
    prefix = prefix.encode("utf-8")[: 255 - len(suffix)].decode("utf-8", errors="ignore")
    return prefix + suffix


def rename_basename(name: str, n: int) -> str:
    base, extension = os.path.splitext(name)
    suffix = f" ({n})"
    extension = extension.encode()[: 255 - len(suffix) - 1].decode(errors="ignore")
    base = base.encode()[: 255 - len(suffix) - len(extension.encode())].decode(errors="ignore")
    return base + suffix + extension


def upload_support_reason() -> str | None:
    if sys.platform == "win32":
        return "Workspace upload requires POSIX filesystem primitives"
    if any(not hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "pwrite", "pread")):
        return "Required upload filesystem primitives are unavailable"
    if not {os.open, os.stat, os.unlink, os.link, os.rename} <= os.supports_dir_fd:
        return "Required directory-relative filesystem operations are unavailable"
    return None


def valid_name(name: str) -> bool:
    return (
        bool(name)
        and len(name.encode("utf-8")) <= 255
        and name not in (".", "..")
        and not any(c in name for c in ("/", "\\"))
        and not re.search(r"[\x00-\x1f\x7f]", name)
        and not is_upload_staging(name)
        and not (
            os.name == "nt"
            and (
                re.search(r'[<>:"|?*\x00-\x1f]', name)
                or name.endswith((".", " "))
                or name.split(".")[0].upper()
                in {
                    "CON",
                    "PRN",
                    "AUX",
                    "NUL",
                    *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10)),
                }
            )
        )
    )


class FrameReader(Protocol):
    def read(self, size: int, /) -> Buffer: ...


class FrameWriter(Protocol):
    def write(self, data: bytes, /) -> object: ...
    def flush(self) -> object: ...


def _exact(stream: FrameReader, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = bytes(stream.read(length - len(data)))
        if not chunk:
            raise EOFError("Incomplete upload frame")
        data.extend(chunk)
    return bytes(data)


def read_frame(stream: FrameReader) -> tuple[dict[str, object], bytes]:
    length = struct.unpack("!I", _exact(stream, 4))[0]
    if not 0 < length <= MAX_METADATA_BYTES:
        raise ValueError("Upload metadata too large")
    metadata = json.loads(_exact(stream, length))
    if not isinstance(metadata, dict):
        raise ValueError("Upload metadata must be an object")
    raw_length = metadata.pop("raw_length", 0)
    if type(raw_length) is not int or not 0 <= raw_length <= MAX_RAW_BYTES:
        raise ValueError("Upload chunk too large")
    return metadata, _exact(stream, raw_length)


def _frame_header(metadata: dict[str, object], raw_length: int) -> bytes:
    if raw_length > MAX_RAW_BYTES:
        raise ValueError("Upload chunk too large")
    header = json.dumps({**metadata, "raw_length": raw_length}, separators=(",", ":")).encode()
    if len(header) > MAX_METADATA_BYTES:
        raise ValueError("Upload metadata too large")
    return header


def write_frame(stream: FrameWriter, metadata: dict[str, object], raw: bytes = b"") -> None:
    header = _frame_header(metadata, len(raw))
    stream.write(struct.pack("!I", len(header)))
    stream.write(header)
    stream.write(raw)
    stream.flush()


class UploadHelperClient(_HelperProcessClient):
    """A separate binary channel with the environment's launcher and policy."""

    def __init__(self, env: CallerProcessOSEnvironment) -> None:
        super().__init__(
            cwd=env.cwd,
            shell_path=env.shell_path,
            sandbox=replace(
                env.sandbox,
                allow_network=False,
                credential_proxy=None,
                egress_relay_port=None,
                egress_socket_path=None,
                env_passthrough=None,
                git_ssh=None,
                git_ssh_socket_path=None,
            ),
            egress_rules=None,
            egress_allow_private_destinations=False,
            copy_on_write_environment=env.copy_on_write_environment,
            upload_raw=True,
        )

    def interrupt(self) -> None:
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(OSError):
                proc.kill()

    def call(
        self,
        metadata: dict[str, object],
        raw: bytes = b"",
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> dict[str, object]:
        """A busy result guarantees no request frame could have been sent."""
        try:
            _frame_header(metadata, len(raw))
        except (TypeError, ValueError) as exc:
            return {"error": str(exc), "code": "invalid_request"}
        timeout_s = operation_timeout(metadata) if timeout_s is None else timeout_s
        deadline = time.monotonic() + timeout_s if deadline is None else deadline
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return {"error": "Upload helper queue timed out", "code": "busy"}
        try:
            if self._closed:
                return {"error": "Upload helper closed", "code": "busy"}
            if time.monotonic() >= deadline:
                return {"error": "Upload helper deadline elapsed", "code": "busy"}
            timed_out, done = threading.Event(), threading.Event()

            def watch() -> None:
                if done.wait(max(0, deadline - time.monotonic())):
                    return
                timed_out.set()
                # Launch can assign the process after the deadline has elapsed.
                while not done.is_set():
                    self.interrupt()
                    done.wait(0.01)

            watchdog = threading.Thread(target=watch, daemon=True)
            watchdog.start()
            frame_started = False
            try:
                self._ensure_started_locked()
                if timed_out.is_set() or time.monotonic() >= deadline:
                    self.interrupt()
                    self._stop_locked()
                    return {"error": "Upload helper launch timed out", "code": "busy"}
                assert self._proc is not None
                assert self._proc.stdin is not None and self._proc.stdout is not None
                frame_started = True
                write_frame(cast(io.TextIOWrapper, self._proc.stdin).buffer, metadata, raw)
                result, _ = read_frame(cast(io.TextIOWrapper, self._proc.stdout).buffer)
                return result
            except (OSError, EOFError, ValueError) as exc:
                self._stop_locked()
                code = (
                    "busy" if not frame_started else ("timeout" if timed_out.is_set() else "gone")
                )
                return {"error": str(exc), "code": code}
            finally:
                done.set()
                watchdog.join()

        finally:
            self._lock.release()


class UploadError(Exception):
    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.result = {"code": code, "error": message, **details}


@dataclass
class _Upload:
    directory: str
    name: str
    staging: str
    fd: int
    parent_fd: int
    parent_identity: tuple[int, int]
    size: int
    conflict: str
    sha256: str | None
    result: dict[str, object] | None = None


def _parent(cwd: Path, directory: str) -> int:
    parts = directory.split("/") if directory else []
    if directory.startswith(("/", "\\")) or any(
        p in ("", ".", "..") or "\\" in p or "\0" in p for p in parts
    ):
        raise UploadError("invalid_path", "Destination must be workspace-relative")
    fd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return st.st_dev, st.st_ino


class UploadEngine:
    """Single-threaded helper dispatch serializes all upload operations."""

    def __init__(self, cwd: Path, sandbox: SandboxPolicy) -> None:
        self.cwd = cwd
        self.sandbox = sandbox
        self.uploads: dict[str, _Upload] = {}
        mask = os.umask(0)
        os.umask(mask)
        self.mode = 0o666 & ~mask

    def _grant_staging(self, directory: str, staging: str) -> None:
        if re.search(r"[\x00-\x1f\x7f]", directory):
            raise UploadError("invalid_path", "Upload path contains control characters")
        path = (self.cwd / directory / staging).resolve()
        _assert_within_reach(self.cwd, self.sandbox, path, need_write=True)
        _assert_write_allowed(self.sandbox, path)

    def _grant(self, directory: str, name: str, staging: str) -> None:
        self._grant_staging(directory, staging)
        path = (self.cwd / directory / name).resolve()
        _assert_within_reach(self.cwd, self.sandbox, path, need_write=True)
        _assert_write_allowed(self.sandbox, path)

    def _check(self, upload: _Upload, *, destination: bool = True) -> None:
        if destination:
            self._grant(upload.directory, upload.name, upload.staging)
        else:
            self._grant_staging(upload.directory, upload.staging)
        parent = _parent(self.cwd, upload.directory)
        try:
            if _identity(parent) != upload.parent_identity:
                raise UploadError("invalid_path", "Destination directory changed")
            if upload.result is None:
                info = os.stat(upload.staging, dir_fd=parent, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != _identity(upload.fd):
                    raise UploadError("invalid_path", "Staging file changed")
        finally:
            os.close(parent)

    def dispatch(self, metadata: dict[str, object], raw: bytes) -> dict[str, object]:
        try:
            return self._dispatch(metadata, raw)
        except UploadError as exc:
            return exc.result
        except PermissionError as exc:
            return {"code": "permission_denied", "error": str(exc)}
        except OSError as exc:
            code = (
                "no_space"
                if exc.errno in (errno.ENOSPC, errno.EDQUOT)
                else "invalid_path"
                if exc.errno
                in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG, errno.EINVAL)
                else "io_error"
            )
            return {"code": code, "error": str(exc)}
        except (KeyError, TypeError, ValueError) as exc:
            return {"code": "invalid_request", "error": str(exc)}

    def _dispatch(self, m: dict[str, object], raw: bytes) -> dict[str, object]:
        op = m["op"]
        if op == "probe":
            reason = upload_support_reason()
            if reason:
                return {"raw_upload": False, "reason": reason}
            _assert_within_reach(self.cwd, self.sandbox, self.cwd, need_write=True)
            _assert_write_allowed(self.sandbox, self.cwd)
            return {"raw_upload": True}
        uid = str(m["id"])
        if not re.fullmatch(r"[0-9a-f]{32}", uid):
            raise UploadError("invalid_request", "Invalid upload id")
        if op == "prepare":
            return self._prepare(uid, m)
        if op == "recover":
            return self._recover(uid, m)
        if op == "begin":
            return self._begin(uid, m)
        if op == "resume":
            return self._resume(uid, m)
        if op == "cleanup":
            return self._cleanup(uid, m)
        if op == "forget":
            upload = self.uploads.get(uid)
            if upload is not None:
                self._grant(upload.directory, upload.name, upload.staging)
                if upload.result is None:
                    raise UploadError("incomplete", "Upload is still open")
                self._forget(uid)
            return {"forgotten": True}
        upload = self.uploads.get(uid)
        if upload is None:
            raise UploadError("gone", "Unknown upload")
        self._check(upload, destination=op != "abort")
        if upload.result is not None:
            return upload.result
        offset = os.fstat(upload.fd).st_size
        if op == "status":
            return {"offset": offset, "size": upload.size}
        if op == "append":
            requested = m["offset"]
            if type(requested) is not int or requested != offset:
                raise UploadError("offset_mismatch", "Unexpected offset", offset=offset)
            if offset + len(raw) > upload.size:
                raise UploadError("too_large", "Declared size exceeded")
            written = 0
            while written < len(raw):
                count = os.pwrite(upload.fd, raw[written:], offset + written)
                if count == 0:
                    raise OSError(errno.ENOSPC, "No write progress")
                written += count
            return {"offset": offset + written}
        if op == "plan":
            return self._plan(upload, offset)
        if op == "complete":
            return self._complete(upload, offset, m.get("publication"))
        if op == "abort":
            os.unlink(upload.staging, dir_fd=upload.parent_fd)
            os.close(upload.fd)
            os.close(upload.parent_fd)
            del self.uploads[uid]
            return {"aborted": True}
        raise UploadError("invalid_request", "Unknown upload operation")

    def _prepare(self, uid: str, m: dict[str, object]) -> dict[str, object]:
        directory, name = str(m["dir"]), str(m["name"])
        if not valid_name(name):
            raise UploadError("invalid_name", "Invalid basename")
        staging = staging_basename(name, uid)
        self._grant(directory, name, staging)
        parent = _parent(self.cwd, directory)
        try:
            self._exists(parent, name)
            try:
                os.stat(staging, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise UploadError("file_exists", "Staging path exists")
            dev, ino = _identity(parent)
            return {
                "parent_dev": dev,
                "parent_ino": ino,
                "staging_path": str(self.cwd / directory / staging),
            }
        finally:
            os.close(parent)

    @staticmethod
    def _publication_result(publication: dict[str, object]) -> dict[str, object]:
        return {
            key: publication[key] for key in ("path", "final_name", "size", "sha256", "outcome")
        }

    def _recover(self, uid: str, m: dict[str, object]) -> dict[str, object]:
        directory, original = str(m["dir"]), str(m["name"])
        publication = m.get("publication")
        if not isinstance(publication, dict):
            raise UploadError("gone", "No durable publication plan")
        name = str(publication["final_name"])
        if not valid_name(original) or not valid_name(name):
            raise UploadError("invalid_name", "Invalid basename")
        staging = staging_basename(original, uid)
        self._grant(directory, name, staging)
        try:
            parent = _parent(self.cwd, directory)
        except (FileNotFoundError, NotADirectoryError) as exc:
            self._forget(uid)
            raise UploadError("parent_gone", "Destination directory disappeared") from exc
        try:
            if _identity(parent) != (m["parent_dev"], m["parent_ino"]):
                self._forget(uid)
                raise UploadError("parent_gone", "Destination directory changed")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError as exc:
                raise UploadError("gone", "Publication has not happened") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode) or _identity(fd) != (
                    m["staging_dev"],
                    m["staging_ino"],
                ):
                    raise UploadError("gone", "No published inode matches this upload")
                os.fchmod(fd, self.mode)
                try:
                    info = os.stat(staging, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if (info.st_dev, info.st_ino) == _identity(fd):
                        os.unlink(staging, dir_fd=parent)
                with contextlib.suppress(OSError):
                    os.fsync(parent)
                self._forget(uid)
                return self._publication_result(publication)
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def _begin(self, uid: str, m: dict[str, object]) -> dict[str, object]:
        directory, name = str(m["dir"]), str(m["name"])
        if not valid_name(name):
            raise UploadError("invalid_name", "Invalid basename")
        if uid in self.uploads:
            raise UploadError("file_exists", "Upload already exists")
        size = m["size"]
        conflict = str(m["on_conflict"])
        digest = m.get("sha256")
        if (
            type(size) is not int
            or size < 0
            or conflict not in ("fail", "replace", "rename")
            or (
                digest is not None
                and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest))
            )
        ):
            raise UploadError("invalid_request", "Invalid upload declaration")
        staging = staging_basename(name, uid)
        self._grant(directory, name, staging)
        parent = _parent(self.cwd, directory)
        try:
            if "parent_dev" in m and _identity(parent) != (m["parent_dev"], m["parent_ino"]):
                raise UploadError("invalid_path", "Destination directory changed")
            if self._exists(parent, name) and conflict == "fail":
                raise UploadError("file_exists", "Destination exists")
            try:
                fd = os.open(
                    staging,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError as exc:
                raise UploadError("file_exists", "Staging path exists") from exc
        except BaseException:
            os.close(parent)
            raise
        pdev, pino = parent_identity = _identity(parent)
        self.uploads[uid] = _Upload(
            directory, name, staging, fd, parent, parent_identity, size, conflict, digest
        )
        dev, ino = _identity(fd)
        return {
            "offset": 0,
            "staging_path": str(self.cwd / directory / staging),
            "staging_dev": dev,
            "staging_ino": ino,
            "parent_dev": pdev,
            "parent_ino": pino,
        }

    def _resume(self, uid: str, m: dict[str, object]) -> dict[str, object]:
        if uid in self.uploads:
            self._check(self.uploads[uid])
            return {"offset": os.fstat(self.uploads[uid].fd).st_size}
        directory, name = str(m["dir"]), str(m["name"])
        if not valid_name(name):
            raise UploadError("invalid_name", "Invalid basename")
        staging = staging_basename(name, uid)
        self._grant(directory, name, staging)
        parent = _parent(self.cwd, directory)
        fd = -1
        try:
            if _identity(parent) != (m["parent_dev"], m["parent_ino"]):
                raise UploadError("invalid_path", "Destination directory changed")
            fd = os.open(staging, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            if not stat.S_ISREG(os.fstat(fd).st_mode) or _identity(fd) != (
                m["staging_dev"],
                m["staging_ino"],
            ):
                raise UploadError("invalid_path", "Staging file changed")
            self.uploads[uid] = _Upload(
                directory,
                name,
                staging,
                fd,
                parent,
                _identity(parent),
                int(str(m["size"])),
                str(m["on_conflict"]),
                cast(str | None, m.get("sha256")),
            )
            return {"offset": os.fstat(fd).st_size}
        except BaseException:
            if fd >= 0:
                os.close(fd)
            os.close(parent)
            raise

    @staticmethod
    def _exists(parent: int, name: str) -> bool:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(info.st_mode):
            raise UploadError("is_directory", "Destination is a directory")
        return True

    def _plan(self, upload: _Upload, offset: int) -> dict[str, object]:
        """Verify bytes and decide publication without modifying the destination."""
        if offset != upload.size:
            raise UploadError("incomplete", "Upload is incomplete")
        digest = hashlib.sha256()
        position = 0
        while block := os.pread(upload.fd, 1024 * 1024, position):
            digest.update(block)
            position += len(block)
        checksum = digest.hexdigest()
        if upload.sha256 is not None and checksum != upload.sha256:
            raise UploadError("checksum_mismatch", "SHA256 differs")
        os.fsync(upload.fd)
        self._check(upload)
        name = upload.name
        existed = self._exists(upload.parent_fd, name)
        if existed and upload.conflict == "fail":
            raise UploadError("file_exists", "Destination appeared")
        if existed and upload.conflict == "rename":
            for n in range(1, MAX_RENAME_ATTEMPTS + 1):
                name = rename_basename(upload.name, n)
                self._grant(upload.directory, name, upload.staging)
                try:
                    os.stat(name, dir_fd=upload.parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    break
            else:
                raise UploadError("file_exists", "No free conflict name")
        identity = None
        if existed and upload.conflict == "replace":
            info = os.stat(name, dir_fd=upload.parent_fd, follow_symlinks=False)
            identity = info.st_dev, info.st_ino
        return {
            "path": f"{upload.directory}/{name}" if upload.directory else name,
            "size": upload.size,
            "sha256": checksum,
            "final_name": name,
            "outcome": "renamed" if name != upload.name else "replaced" if identity else "created",
            "target_dev": identity[0] if identity else None,
            "target_ino": identity[1] if identity else None,
        }

    def _complete(self, upload: _Upload, offset: int, publication: object) -> dict[str, object]:
        """SHA-256 covers bytes read during the final pre-publication re-check.
        A session writer can change bytes during that read or after publication, so
        the returned checksum can be stale.
        """
        plan = publication if isinstance(publication, dict) else self._plan(upload, offset)
        name = str(plan["final_name"])
        if not valid_name(name) or plan["size"] != upload.size or offset != upload.size:
            raise UploadError("invalid_request", "Publication plan changed")
        self._grant(upload.directory, name, upload.staging)
        self._check(upload)
        if upload.conflict == "replace":
            try:
                info = os.stat(name, dir_fd=upload.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                identity = None, None
            else:
                if stat.S_ISDIR(info.st_mode):
                    raise UploadError("is_directory", "Destination is a directory")
                identity = info.st_dev, info.st_ino
            if identity != (plan["target_dev"], plan["target_ino"]):
                raise UploadError("plan_changed", "Destination changed before publication")

        os.fsync(upload.fd)
        digest = hashlib.sha256()
        position = 0
        while block := os.pread(upload.fd, 1024 * 1024, position):
            digest.update(block)
            position += len(block)
        checksum = digest.hexdigest()
        if (
            position != upload.size
            or os.fstat(upload.fd).st_size != upload.size
            or (plan.get("sha256") is not None and checksum != plan["sha256"])
            or (upload.sha256 is not None and checksum != upload.sha256)
        ):
            raise UploadError("checksum_mismatch", "Staging bytes changed before publication")

        if upload.conflict == "replace":
            os.replace(
                upload.staging, name, src_dir_fd=upload.parent_fd, dst_dir_fd=upload.parent_fd
            )
        else:
            try:
                os.link(
                    upload.staging,
                    name,
                    src_dir_fd=upload.parent_fd,
                    dst_dir_fd=upload.parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise UploadError(
                    "plan_changed" if upload.conflict == "rename" else "file_exists",
                    "Destination appeared",
                ) from exc
            os.unlink(upload.staging, dir_fd=upload.parent_fd)
        os.fchmod(upload.fd, self.mode)
        with contextlib.suppress(OSError):
            os.fsync(upload.parent_fd)
        upload.result = self._publication_result(plan)
        os.close(upload.fd)
        os.close(upload.parent_fd)
        upload.fd = upload.parent_fd = -1
        return upload.result

    def _forget(self, uid: str) -> None:
        upload = self.uploads.pop(uid, None)
        if upload is not None:
            for fd in (upload.fd, upload.parent_fd):
                if fd >= 0:
                    os.close(fd)

    def _cleanup(self, uid: str, m: dict[str, object]) -> dict[str, object]:
        directory, name = str(m["dir"]), str(m["name"])
        if not valid_name(name):
            raise UploadError("invalid_name", "Invalid basename")
        staging = staging_basename(name, uid)
        self._grant_staging(directory, staging)
        try:
            parent = _parent(self.cwd, directory)
        except (FileNotFoundError, NotADirectoryError) as exc:
            self._forget(uid)
            raise UploadError("parent_gone", "Destination directory disappeared") from exc
        try:
            if _identity(parent) != (m["parent_dev"], m["parent_ino"]):
                self._forget(uid)
                raise UploadError("parent_gone", "Destination directory changed")
            try:
                info = os.stat(staging, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                self._forget(uid)
                return {"aborted": True}
            if not m.get("staging_dev") or not m.get("staging_ino"):
                raise UploadError(
                    "unverified_intent", "Leaving staging intent without an inode identity"
                )
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (
                m["staging_dev"],
                m["staging_ino"],
            ):
                raise UploadError("staging_gone", "Staging file changed")
            fd = os.open(staging, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if _identity(fd) != (info.st_dev, info.st_ino):
                    raise UploadError("staging_gone", "Staging file changed")
                current = os.stat(staging, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != _identity(fd):
                    raise UploadError("staging_gone", "Staging file changed before cleanup")
                os.unlink(staging, dir_fd=parent)
            finally:
                os.close(fd)
            self._forget(uid)
            return {"aborted": True}
        except UploadError as exc:
            if exc.result["code"] == "staging_gone":
                self._forget(uid)
            raise
        finally:
            os.close(parent)

    def close(self) -> None:
        for upload in self.uploads.values():
            if upload.fd >= 0:
                os.close(upload.fd)
            if upload.parent_fd >= 0:
                os.close(upload.parent_fd)
        self.uploads.clear()


def serve_uploads(cwd: Path, sandbox: SandboxPolicy) -> int:
    engine = UploadEngine(cwd, sandbox)
    try:
        while True:
            try:
                metadata, raw = read_frame(sys.stdin.buffer)
            except EOFError:
                break
            write_frame(sys.stdout.buffer, engine.dispatch(metadata, raw))
    finally:
        engine.close()
    return 0
