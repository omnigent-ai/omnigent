"""S3-compatible implementation of ArtifactStore.

Stores artifact blobs in any S3-compatible object store — AWS S3,
Cloudflare R2, MinIO, Google Cloud Storage (S3 API), etc. — over the
network, so the artifact store survives an ephemeral or multi-replica
deployment (no shared filesystem / FUSE mount required).

Credentials and the endpoint come from the standard AWS environment that
``boto3`` reads:

- ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` — credentials.
- ``AWS_ENDPOINT_URL_S3`` (or ``AWS_ENDPOINT_URL``) — the S3 endpoint for a
  non-AWS provider, e.g. ``https://<account>.r2.cloudflarestorage.com`` for
  Cloudflare R2. Leave unset for AWS S3.
- ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` — region (R2 uses ``auto``).

Storage location format::

    s3://<bucket>[/<prefix>]

Requirements::

    pip install boto3
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from omnigent.stores.artifact_store import ArtifactStore


def _ensure_boto3() -> None:
    """
    Verify that ``boto3`` is installed.

    :raises ImportError: If the package is not available.
    """
    try:
        import boto3  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "S3ArtifactStore requires 'boto3'. Install with: pip install boto3 "
            "(or 'pip install omnigent[s3]')."
        ) from exc


def _parse_s3_uri(storage_location: str) -> tuple[str, str]:
    """
    Split an ``s3://bucket/prefix`` URI into ``(bucket, prefix)``.

    :param storage_location: The full URI, e.g. ``"s3://my-bucket/artifacts"``.
    :returns: ``(bucket, prefix)`` where *prefix* is ``""`` when absent and
        never has leading/trailing slashes, e.g. ``("my-bucket", "artifacts")``.
    :raises ValueError: If the URI doesn't start with ``s3://`` or has no bucket.
    """
    if not storage_location.startswith("s3://"):
        raise ValueError(f"storage_location must start with 's3://', got: {storage_location!r}")
    bucket, _, prefix = storage_location[len("s3://") :].partition("/")
    if not bucket:
        raise ValueError(f"storage_location is missing a bucket: {storage_location!r}")
    return bucket, prefix.strip("/")


def _validate_key(key: str) -> None:
    """
    Validate an artifact key against traversal attacks.

    Same validation as ``LocalArtifactStore`` / ``DatabricksVolumesArtifactStore``
    — reject empty keys, ``..`` sequences, backslashes, and absolute paths — so a
    crafted key can't escape the configured prefix.

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


def _is_not_found(exc: Any) -> bool:
    """
    Whether a botocore ``ClientError`` is a "no such object" error.

    Covers both ``get_object`` (``NoSuchKey``) and ``head_object`` (a bare
    ``404`` with no ``NoSuchKey`` code), across providers.

    :param exc: A ``botocore.exceptions.ClientError``.
    :returns: ``True`` if the error means the object does not exist.
    """
    err = getattr(exc, "response", {}) or {}
    code = err.get("Error", {}).get("Code", "")
    status = err.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "NoSuchBucket", "404", "NotFound") or status == 404


def _make_client() -> Any:
    """
    Build a boto3 S3 client from the ambient AWS environment.

    Endpoint and region come from env vars (see the module docstring) so the
    same backend serves AWS S3, R2, MinIO, etc. without provider-specific code.

    :returns: A configured ``boto3`` S3 client.
    """
    _ensure_boto3()
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "auto"
    return boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


class S3ArtifactStore(ArtifactStore):
    """
    Stores binary blobs in an S3-compatible bucket.

    All I/O goes through the S3 API (``boto3``) — no local filesystem or FUSE
    mount — so the store is durable and shared across replicas. The
    ``storage_location`` is an ``s3://bucket[/prefix]`` URI; keys are stored
    under the prefix::

        s3://my-bucket/artifacts/
            agents/agent_abc123/bundle.tar.gz
            executor_storage/conv_123/agent.tar.gz

    :param storage_location: S3 URI, e.g. ``"s3://my-bucket/artifacts"``.
    :param client: Optional pre-built boto3 S3 client (for tests or to inject a
        custom-authenticated client). When omitted, one is built from the
        ambient AWS environment.
    """

    def __init__(self, storage_location: str, client: Any | None = None) -> None:
        """
        Initialize the S3 artifact store.

        :param storage_location: S3 URI, e.g. ``"s3://my-bucket/artifacts"``.
        :param client: Optional pre-built boto3 S3 client.
        :raises ImportError: If ``boto3`` is not installed.
        :raises ValueError: If the URI format is invalid.
        """
        _ensure_boto3()
        super().__init__(storage_location)
        self._bucket, self._prefix = _parse_s3_uri(storage_location)
        self._client = client if client is not None else _make_client()

    def _resolve(self, key: str) -> str:
        """
        Map an artifact key to a full S3 object key (prefix + key).

        :param key: Forward-slash-separated artifact key.
        :returns: The S3 object key, e.g. ``"artifacts/agents/abc/bundle.tar.gz"``.
        :raises ValueError: If the key is invalid.
        """
        _validate_key(key)
        return f"{self._prefix}/{key}" if self._prefix else key

    # ── ArtifactStore interface ──────────────────────────────

    def put(self, key: str, data: bytes) -> None:
        """
        Upload bytes to the object store. Overwrites if the key exists.

        :param key: Forward-slash-separated artifact key.
        :param data: Raw bytes to store.
        """
        self._client.put_object(Bucket=self._bucket, Key=self._resolve(key), Body=data)

    def get(self, key: str) -> bytes:
        """
        Download bytes for a key.

        :param key: Forward-slash-separated artifact key.
        :returns: The raw bytes of the stored blob.
        :raises KeyError: If no object exists for the key.
        """
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._resolve(key))
            return resp["Body"].read()
        except ClientError as exc:
            if _is_not_found(exc):
                raise KeyError(key) from None
            raise

    def delete(self, key: str) -> None:
        """
        Remove an object. No-op if the key does not exist (S3 ``DeleteObject``
        is idempotent).

        :param key: Forward-slash-separated artifact key.
        """
        self._client.delete_object(Bucket=self._bucket, Key=self._resolve(key))

    def exists(self, key: str) -> bool:
        """
        Check whether an object exists, via a ``HEAD`` request (no download).

        :param key: Forward-slash-separated artifact key.
        :returns: ``True`` if the object exists, ``False`` otherwise.
        """
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=self._resolve(key))
            return True
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise
