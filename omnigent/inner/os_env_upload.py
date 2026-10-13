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
from collections.abc import Buffer
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from omnigent.inner.os_env import (
    CallerProcessOSEnvironment,
    _assert_within_reach,
    _assert_write_allowed,
    _HelperProcessClient,
)
from omnigent.inner.sandbox import SandboxPolicy

MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
STAGING_SUFFIX_PATTERN = r"\.omnigent-upload-[0-9a-f]{32}\.part\Z"


def is_upload_staging(path: str) -> bool:
    return any(re.search(STAGING_SUFFIX_PATTERN, part) for part in path.split("/"))


def staging_basename(name: str, uid: str) -> str:
    suffix = f".omnigent-upload-{uid}.part"
    # Existing dotfiles are masked when a fresh sandboxed helper starts.
    prefix = name.lstrip(".") or "upload"
    prefix = prefix.encode("utf-8")[: 255 - len(suffix)].decode("utf-8", errors="ignore")
    return prefix + suffix


def valid_name(name: str) -> bool:
    return (
        bool(name)
        and name not in (".", "..")
        and not any(c in name for c in ("/", "\\", "\0"))
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


def write_frame(stream: FrameWriter, metadata: dict[str, object], raw: bytes = b"") -> None:
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError("Upload chunk too large")
    header = json.dumps({**metadata, "raw_length": len(raw)}, separators=(",", ":")).encode()
    if len(header) > MAX_METADATA_BYTES:
        raise ValueError("Upload metadata too large")
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
            sandbox=env.sandbox,
            egress_rules=env._egress_rules,
            egress_allow_private_destinations=env._egress_allow_private_destinations,
            copy_on_write_environment=env.copy_on_write_environment,
            upload_raw=True,
        )

    def call(self, metadata: dict[str, object], raw: bytes = b"") -> dict[str, object]:
        with self._lock:
            if self._closed:
                return {"error": "Upload helper closed", "code": "gone"}
            try:
                self._ensure_started_locked()
                assert self._proc is not None
                assert self._proc.stdin is not None and self._proc.stdout is not None
                write_frame(cast(io.TextIOWrapper, self._proc.stdin).buffer, metadata, raw)
                result, _ = read_frame(cast(io.TextIOWrapper, self._proc.stdout).buffer)
                return result
            except (OSError, EOFError, ValueError) as exc:
                self._stop_locked()
                return {"error": str(exc), "code": "gone"}


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

    def _grant(self, directory: str, name: str, staging: str) -> None:
        for basename in (name, staging):
            path = (self.cwd / directory / basename).resolve()
            _assert_within_reach(self.cwd, self.sandbox, path, need_write=True)
            _assert_write_allowed(self.sandbox, path)

    def _check(self, upload: _Upload) -> None:
        self._grant(upload.directory, upload.name, upload.staging)
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
            code = "no_space" if exc.errno in (errno.ENOSPC, errno.EDQUOT) else "invalid_path"
            return {"code": code, "error": str(exc)}
        except (KeyError, TypeError, ValueError) as exc:
            return {"code": "invalid_request", "error": str(exc)}

    def _dispatch(self, m: dict[str, object], raw: bytes) -> dict[str, object]:
        op = m["op"]
        if op == "probe":
            _assert_within_reach(self.cwd, self.sandbox, self.cwd, need_write=True)
            _assert_write_allowed(self.sandbox, self.cwd)
            return {"raw_upload": True}
        uid = str(m["id"])
        if not re.fullmatch(r"[0-9a-f]{32}", uid):
            raise UploadError("invalid_request", "Invalid upload id")
        if op == "begin":
            return self._begin(uid, m)
        if op == "cleanup":
            return self._cleanup(uid, m)
        upload = self.uploads.get(uid)
        if upload is None:
            raise UploadError("gone", "Unknown upload")
        self._check(upload)
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
        if op == "complete":
            return self._complete(upload, offset)
        if op == "abort":
            os.unlink(upload.staging, dir_fd=upload.parent_fd)
            os.close(upload.fd)
            os.close(upload.parent_fd)
            del self.uploads[uid]
            return {"aborted": True}
        raise UploadError("invalid_request", "Unknown upload operation")

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

    @staticmethod
    def _exists(parent: int, name: str) -> bool:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(info.st_mode):
            raise UploadError("is_directory", "Destination is a directory")
        return True

    def _complete(self, upload: _Upload, offset: int) -> dict[str, object]:
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
        if upload.conflict == "replace":
            os.replace(
                upload.staging, name, src_dir_fd=upload.parent_fd, dst_dir_fd=upload.parent_fd
            )
            outcome = "replaced" if existed else "created"
        else:
            n = 0
            while True:
                self._grant(upload.directory, name, upload.staging)
                try:
                    os.link(
                        upload.staging,
                        name,
                        src_dir_fd=upload.parent_fd,
                        dst_dir_fd=upload.parent_fd,
                        follow_symlinks=False,
                    )
                    break
                except FileExistsError as exc:
                    self._exists(upload.parent_fd, name)
                    if upload.conflict == "fail":
                        raise UploadError("file_exists", "Destination appeared") from exc
                    n += 1
                    base, ext = os.path.splitext(upload.name)
                    name = f"{base} ({n}){ext}"
            os.unlink(upload.staging, dir_fd=upload.parent_fd)
            outcome = "renamed" if n else "created"
        os.fchmod(upload.fd, self.mode)
        with contextlib.suppress(OSError):
            os.fsync(upload.parent_fd)
        upload.result = {
            "path": f"{upload.directory}/{name}" if upload.directory else name,
            "size": upload.size,
            "sha256": checksum,
            "outcome": outcome,
            "final_name": name,
        }
        os.close(upload.fd)
        os.close(upload.parent_fd)
        upload.fd = upload.parent_fd = -1
        return upload.result

    def _cleanup(self, uid: str, m: dict[str, object]) -> dict[str, object]:
        if uid in self.uploads:
            result = self._dispatch({"op": "abort", "id": uid}, b"")
            self.uploads.pop(uid, None)
            return result
        directory, name = str(m["dir"]), str(m["name"])
        if not valid_name(name):
            raise UploadError("invalid_name", "Invalid basename")
        staging = staging_basename(name, uid)
        self._grant(directory, name, staging)
        parent = _parent(self.cwd, directory)
        try:
            if _identity(parent) != (m["parent_dev"], m["parent_ino"]):
                raise UploadError("invalid_path", "Destination directory changed")
            try:
                info = os.stat(staging, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return {"aborted": True}
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (
                m["staging_dev"],
                m["staging_ino"],
            ):
                raise UploadError("invalid_path", "Staging file changed")
            os.unlink(staging, dir_fd=parent)
            return {"aborted": True}
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
