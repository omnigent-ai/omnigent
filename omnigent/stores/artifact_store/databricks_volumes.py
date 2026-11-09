"""Databricks UC Volumes implementation of ArtifactStore.

Uses the Databricks SDK ``WorkspaceClient.files`` API for all
operations. Authentication uses ambient workspace credentials
(automatic in Databricks Apps, or via ``DATABRICKS_HOST`` +
``DATABRICKS_TOKEN`` locally).

Storage location format::

    dbfs:/Volumes/<catalog>/<schema>/<volume>[/<prefix>]

Requirements::

    pip install databricks-sdk
"""

from __future__ import annotations

import contextlib
import io
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from omnigent.stores.artifact_store import ArtifactStore
from omnigent.util.databricks_sdk import workspace_client_with_cli_cache

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient


def _ensure_databricks_sdk() -> None:
    """
    Verify that ``databricks-sdk`` is installed.

    :raises ImportError: If the package is not available.
    """
    try:
        import databricks.sdk  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "DatabricksVolumesArtifactStore requires 'databricks-sdk'. "
            "Install with: pip install databricks-sdk"
        ) from exc


def _parse_volume_root(storage_location: str) -> str:
    """
    Extract the volume root path from a ``dbfs:/Volumes/...`` URI.

    Strips the ``dbfs:`` scheme prefix, yielding a path like
    ``/Volumes/catalog/schema/volume/prefix``.

    :param storage_location: The full URI, e.g.
        ``"dbfs:/Volumes/my_catalog/my_schema/my_volume/artifacts"``.
    :returns: The path portion without the scheme, e.g.
        ``"/Volumes/my_catalog/my_schema/my_volume/artifacts"``.
    :raises ValueError: If the URI doesn't start with
        ``"dbfs:/Volumes/"``.
    """
    if not storage_location.startswith("dbfs:/Volumes/"):
        raise ValueError(
            f"storage_location must start with 'dbfs:/Volumes/', got: {storage_location!r}"
        )
    # Strip "dbfs:" prefix, keep the /Volumes/... path
    return storage_location[len("dbfs:") :]


def _validate_key(key: str) -> None:
    """
    Validate an artifact key against traversal attacks.

    Same validation as ``LocalArtifactStore._resolve`` — reject
    empty keys, ``..`` sequences, backslashes, and absolute paths.

    :param key: Forward-slash-separated artifact key, e.g.
        ``"agents/agent_abc123/bundle.tar.gz"``.
    :raises ValueError: If the key is invalid.
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


class DatabricksVolumesArtifactStore(ArtifactStore):
    """
    Stores binary blobs in a Databricks Unity Catalog Volume.

    Uses the ``WorkspaceClient.files`` API for remote file
    operations. No FUSE mount required — all I/O goes through
    the Databricks REST API.

    The ``storage_location`` is a ``dbfs:/Volumes/...`` URI.
    Keys are appended to the volume root path::

        dbfs:/Volumes/catalog/schema/volume/prefix/
            agents/agent_abc123/bundle.tar.gz
            executor_storage/conv_123/agent.tar.gz

    :param storage_location: UC Volume URI, e.g.
        ``"dbfs:/Volumes/my_catalog/my_schema/my_volume/artifacts"``.
    """

    def __init__(
        self, storage_location: str, workspace_client: WorkspaceClient | None = None
    ) -> None:
        """
        Initialize the UC Volumes artifact store.

        *workspace_client*, when provided, is used as-is — letting a caller
        inject a pre-authenticated client (e.g. one scoped to a specific
        principal or workspace). When omitted, a ``WorkspaceClient`` is
        created from ambient credentials (``DATABRICKS_HOST``/``DATABRICKS_TOKEN``
        env vars, or automatic in Databricks Apps).

        :param storage_location: UC Volume URI, e.g.
            ``"dbfs:/Volumes/my_catalog/my_schema/my_volume/artifacts"``.
        :param workspace_client: Optional pre-authenticated ``WorkspaceClient``.
        :raises ImportError: If ``databricks-sdk`` is not installed.
        :raises ValueError: If the URI format is invalid.
        """
        _ensure_databricks_sdk()
        super().__init__(storage_location)
        self._root = _parse_volume_root(storage_location)
        if workspace_client is not None:
            self._client = workspace_client
        else:
            from databricks.sdk import WorkspaceClient

            self._client = workspace_client_with_cli_cache(WorkspaceClient)

    def _resolve(self, key: str) -> str:
        """
        Map a key to a full UC Volume file path.

        :param key: Forward-slash-separated artifact key, e.g.
            ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: Full volume path, e.g.
            ``"/Volumes/catalog/schema/volume/prefix/agents/agent_abc123/bundle.tar.gz"``.
        :raises ValueError: If the key is invalid.
        """
        _validate_key(key)
        return f"{self._root}/{key}"

    # ── ArtifactStore interface ──────────────────────────────

    def put(self, key: str, data: bytes) -> None:
        """
        Upload bytes to a file in the UC Volume.

        Overwrites if the file already exists. Parent directories
        are created automatically by the Databricks API.

        :param key: Forward-slash-separated artifact key, e.g.
            ``"agents/agent_abc123/bundle.tar.gz"``.
        :param data: Raw bytes to store.
        """
        path = self._resolve(key)
        self._client.files.upload(path, io.BytesIO(data), overwrite=True)

    def get(self, key: str) -> bytes:
        """
        Download bytes from a file in the UC Volume.

        :param key: Forward-slash-separated artifact key, e.g.
            ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: The raw bytes of the stored blob.
        :raises KeyError: If no file exists at the resolved path.
        """
        path = self._resolve(key)
        from databricks.sdk.errors import NotFound

        try:
            resp = self._client.files.download(path)
            contents = resp.contents
            if contents is None:
                raise KeyError(key)
            return contents.read()
        except NotFound:
            raise KeyError(key) from None

    def delete(self, key: str) -> None:
        """
        Remove a file from the UC Volume. No-op if the file does
        not exist.

        :param key: Forward-slash-separated artifact key, e.g.
            ``"agents/agent_abc123/bundle.tar.gz"``.
        """
        path = self._resolve(key)
        from databricks.sdk.errors import NotFound

        with contextlib.suppress(NotFound):
            self._client.files.delete(path)

    def exists(self, key: str) -> bool:
        """
        Check whether a file exists in the UC Volume.

        Uses ``get_metadata`` (HEAD request) — does not download
        the file contents.

        :param key: Forward-slash-separated artifact key, e.g.
            ``"agents/agent_abc123/bundle.tar.gz"``.
        :returns: ``True`` if the file exists, ``False`` otherwise.
        """
        path = self._resolve(key)
        from databricks.sdk.errors import NotFound

        try:
            self._client.files.get_metadata(path)
            return True
        except NotFound:
            return False
