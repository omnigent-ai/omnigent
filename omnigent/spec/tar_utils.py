"""Safe tarball extraction for agent image bundles."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path, PurePosixPath

# 500 MB default limit to guard against decompression bombs
DEFAULT_MAX_BYTES = 500 * 1024 * 1024

# Maximum number of entries to prevent zip-bomb style attacks
DEFAULT_MAX_ENTRIES = 10_000


def normalize_tarinfo(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """Remove host-specific metadata so identical agent directories hash alike."""
    tarinfo.mtime = 0
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = ""
    tarinfo.mode = 0o755 if tarinfo.isdir() else 0o644
    return tarinfo


def pack_directory(bundle_dir: Path) -> bytes:
    """Pack a materialized agent directory into a reproducible gzipped tarball."""
    output = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        archive.add(str(bundle_dir), arcname=".", filter=normalize_tarinfo)
    return output.getvalue()


class ExtractionError(Exception):
    """
    Raised when a tarball fails safety checks during extraction.

    Safety violations include path traversal, symlinks/hardlinks,
    special file types (FIFOs, character/block devices), decompression
    bombs, and entry count bombs.
    """


def extract_safe(
    source: Path | bytes,
    dest: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> Path:
    """
    Extract a tarball to *dest* with safety checks.

    Rejects:

    - Path traversal (members outside *dest* via ``..`` or
      absolute paths)
    - Symlinks and hardlinks (could escape *dest*)
    - Special file types -- FIFOs, character/block devices,
      sockets (only regular files and directories are allowed).
      A FIFO smuggled in as ``config.yaml`` would otherwise be
      created on disk and hang the worker forever when the spec
      loader calls ``read_text()`` on it.
    - Decompression bombs (total extracted bytes > *max_bytes*)
    - Entry count bombs (more than *max_entries* files)

    :param source: Path to a ``.tar.gz`` / ``.tar`` file, or raw
        tarball bytes (e.g. from an HTTP upload).
    :param dest: Directory to extract into (created if needed).
    :param max_bytes: Maximum total extracted size in bytes.
        Defaults to 500 MB.
    :param max_entries: Maximum number of tar entries. Defaults
        to 10,000.
    :returns: The *dest* path for convenience.
    :raises ExtractionError: If any safety check fails.
    :raises FileNotFoundError: If *source* is a :class:`Path` and
        does not exist.
    """
    if isinstance(source, Path):
        if not source.exists():
            raise FileNotFoundError(f"tarball not found: {source}")

    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()

    total_bytes = 0
    entry_count = 0

    with _open_tar(source) as tf:
        for member in tf:
            entry_count += 1
            if entry_count > max_entries:
                raise ExtractionError(f"tarball exceeds max entry count ({max_entries})")

            _check_member_safety(member, resolved_dest)

            if member.isfile():
                total_bytes += member.size
                if total_bytes > max_bytes:
                    raise ExtractionError(
                        f"tarball exceeds max extracted size ({max_bytes} bytes)"
                    )

            # Defense in depth on top of _check_member_safety: only
            # regular files and directories reach this point, so
            # ``filter='data'`` (Python >= 3.12) is a no-op for valid
            # members but rejects anything malicious that slipped past
            # the explicit checks above.
            tf.extract(member, dest, set_attrs=False, filter="data")

    return dest


def _open_tar(source: Path | bytes) -> tarfile.TarFile:
    """
    Open a tarball from a file path or raw bytes.

    :param source: Path to a ``.tar.gz`` / ``.tar`` file, or raw
        tarball bytes.
    :returns: An opened :class:`tarfile.TarFile` ready for
        iteration.
    :raises ExtractionError: If the data is not a valid tarball.
    """
    try:
        if isinstance(source, bytes):
            return tarfile.open(fileobj=io.BytesIO(source), mode="r:*")
        return tarfile.open(source, "r:*")
    except (tarfile.ReadError, tarfile.CompressionError) as exc:
        raise ExtractionError(f"invalid tarball: {exc}") from exc


def _check_member_safety(member: tarfile.TarInfo, resolved_dest: Path) -> None:
    """
    Validate a single tar member against safety rules.

    :param member: The :class:`tarfile.TarInfo` entry to validate.
    :param resolved_dest: The resolved (absolute) extraction
        destination directory, used for containment checks.
    :raises ExtractionError: If the member is not a regular file or
        directory (symlink, hardlink, FIFO, device, socket), uses an
        absolute path, contains ``..`` traversal, or resolves outside
        *resolved_dest*.
    """
    # Allow-list the member type: only regular files and directories
    # may be extracted. A deny-list (block links only) is what let
    # FIFOs / device nodes through -- a FIFO named
    # ``config.yaml`` passes ``exists()`` but hangs the worker forever
    # on ``read_text()``. issym()/islnk() are covered by this check
    # (a link is neither isfile() nor isdir()); they keep a dedicated
    # branch below only for a clearer error message.
    if not (member.isfile() or member.isdir()):
        if member.issym() or member.islnk():
            raise ExtractionError(f"tarball contains a link (not allowed): {member.name!r}")
        raise ExtractionError(
            f"tarball contains an unsupported entry type "
            f"(only regular files and directories are allowed): "
            f"{member.name!r}"
        )

    # Reject absolute paths
    if PurePosixPath(member.name).is_absolute():
        raise ExtractionError(f"tarball contains an absolute path: {member.name!r}")

    # Reject path traversal via .. segments
    parts = PurePosixPath(member.name).parts
    if ".." in parts:
        raise ExtractionError(f"tarball contains path traversal: {member.name!r}")

    # Post-resolution containment check
    resolved_target = (resolved_dest / member.name).resolve()
    if not resolved_target.is_relative_to(resolved_dest):
        raise ExtractionError(f"tarball member escapes destination: {member.name!r}")
