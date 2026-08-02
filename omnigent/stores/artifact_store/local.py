"""Local filesystem implementation of ArtifactStore."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from omnigent.stores.artifact_store import ArtifactStore


def _io_path(path: Path) -> Path:
    """Return a Windows extended-length path for filesystem I/O."""
    if os.name != "nt":
        return path
    raw_path = str(path)
    if raw_path.startswith("\\\\?\\"):
        return path
    if raw_path.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw_path[2:])
    return Path("\\\\?\\" + raw_path)


class LocalArtifactStore(ArtifactStore):
    """
    Stores binary blobs as flat files under a local directory.

    The ``storage_location`` is a filesystem path used as the root
    directory.  Layout::

        storage_location/
            <key1>
            nested/key2
            ...

    Keys use forward slashes as separators and are mapped to the
    native OS path on disk.  Traversal sequences (``..``) and
    backslashes are rejected; a post-resolution containment check
    ensures the resolved path stays within the root even if symlinks
    are involved.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the local artifact store.

        Creates the root directory if it does not exist.

        :param storage_location: Filesystem path to the root
            directory, e.g. ``"/data/artifacts"``.
        """
        super().__init__(storage_location)
        self._root = Path(storage_location)
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """
        Map *key* (forward-slash separated) to an absolute
        filesystem path.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: The resolved absolute :class:`Path`.
        :raises ValueError: If the key is empty, contains
            traversal sequences (``..``), backslashes, or
            resolves outside the root directory.
        """
        parts = PurePosixPath(key).parts
        if (
            not parts
            or ".." in parts
            or "\\" in key
            or PurePosixPath(key).is_absolute()
            or PureWindowsPath(key).is_absolute()
        ):
            raise ValueError(f"invalid artifact key: {key!r}")

        # Join validated parts with OS-native separator
        resolved = (self._root / Path(*parts)).resolve()
        if not resolved.is_relative_to(self._root.resolve()):
            raise ValueError(f"artifact key escapes root directory: {key!r}")
        return resolved

    # ── ArtifactStore interface ──────────────────────────────

    def put(self, key: str, data: bytes) -> None:
        """
        Write bytes to a file under the root directory.

        Creates intermediate directories as needed. Overwrites
        the file if it already exists.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :param data: Raw bytes to write.
        """
        path = _io_path(self._resolve(key))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        """
        Read bytes from a file under the root directory.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: The raw bytes of the file.
        :raises KeyError: If no file exists at the resolved path.
        """
        path = _io_path(self._resolve(key))
        if not path.exists():
            raise KeyError(key)
        return path.read_bytes()

    def delete(self, key: str) -> None:
        """
        Remove a file under the root directory. No-op if the file
        does not exist.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        """
        path = _io_path(self._resolve(key))
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        """
        Check whether a file exists under the root directory.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: ``True`` if the file exists, ``False`` otherwise.
        """
        return _io_path(self._resolve(key)).exists()
