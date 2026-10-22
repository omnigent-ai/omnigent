"""Local filesystem implementation of ArtifactStore."""

from __future__ import annotations

import os
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from omnigent.stores.artifact_store import ArtifactStore


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

        Creates intermediate directories as needed. Overwrites the file
        if it already exists. Writes to a sibling temp file first and
        ``os.replace``s it into place: a concurrent :meth:`get` of the
        same key (e.g. a re-``put`` of a builtin/managed agent racing an
        in-flight cache load of the same bundle) always observes either
        the old complete content or the new complete content, never a
        truncated read from a plain overwrite-in-place.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :param data: Raw bytes to write.
        """
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        """
        Read bytes from a file under the root directory.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: The raw bytes of the file.
        :raises KeyError: If no file exists at the resolved path.
        """
        path = self._resolve(key)
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
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        """
        Check whether a file exists under the root directory.

        :param key: Forward-slash-separated artifact key,
            e.g. ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: ``True`` if the file exists, ``False`` otherwise.
        """
        return self._resolve(key).exists()
