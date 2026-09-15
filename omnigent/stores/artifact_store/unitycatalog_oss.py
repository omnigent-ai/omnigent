"""Unity Catalog OSS implementation of ArtifactStore.

Stores artifact blobs in a volume managed by an **open-source** Unity
Catalog server (github.com/unitycatalog/unitycatalog), NOT managed
Databricks. It mirrors the shape of the sibling ``s3.py`` and
``databricks_volumes.py`` backends but talks to the UC OSS REST API over
plain HTTP (via ``httpx``, already a core dependency) — no boto3, no
Databricks SDK.

How bytes are stored
--------------------
The managed-Databricks backend uploads file bytes through the
``WorkspaceClient.files`` REST API; the S3 backend uses ``boto3``.
**UC OSS has neither** — its REST surface manages *metadata* (catalogs,
schemas, volumes, tables) plus, for cloud volumes, temporary
storage-credential vending. The bytes themselves live at the volume's
``storage_location``.

For a **local** UC OSS deployment a volume's ``storage_location`` is a
``file://`` URI (e.g. ``file:///home/unitycatalog/etc/data/skillpacks``).
There are no cloud credentials to vend for a ``file://`` volume, so this
store resolves the volume's ``storage_location`` from the REST API (the
local analogue of credential vending) and then reads/writes the blob
bytes directly on that filesystem. When the UC server runs in Docker,
that path is made host-visible with a bind mount and supplied as
``local_root`` (see ``examples/uc-oss-skillpack``).

Storage location format::

    <catalog>.<schema>.<volume>

i.e. the three-level volume name the UC REST API keys on, e.g.
``unity.omnigent.skillpacks`` — unlike the ``dbfs:/Volumes/...`` /
``s3://...`` URIs the sibling backends use.

POC / not for production. See ``examples/uc-oss-skillpack/README.md``.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from omnigent.stores.artifact_store import ArtifactStore

if TYPE_CHECKING:
    import httpx

# REST API root for a UC OSS server. The ``/api/2.1/unity-catalog`` prefix
# is fixed by the OSS OpenAPI spec (api/all.yaml ``servers:`` in the
# unitycatalog repo).
_UC_API_PREFIX = "/api/2.1/unity-catalog"
_DEFAULT_URI = "http://localhost:8080"


def _ensure_httpx() -> None:
    """
    Verify that ``httpx`` is importable.

    ``httpx`` is a core dependency, so this should never fail in a
    correctly-installed environment. Mirrors the ``_ensure_boto3`` pattern
    in ``s3.py`` — turns a confusing ``ModuleNotFoundError`` deep in the
    stack into a clear message.

    :raises ImportError: If ``httpx`` is not available.
    """
    try:
        import httpx  # noqa: F401
    except ImportError as exc:  # pragma: no cover - httpx is a core dep
        raise ImportError(
            "UnityCatalogOSSArtifactStore requires 'httpx'. Install with: pip install httpx"
        ) from exc


def _parse_volume_full_name(storage_location: str) -> tuple[str, str, str]:
    """
    Split a three-level volume name into ``(catalog, schema, volume)``.

    :param storage_location: The fully-qualified volume name, e.g.
        ``"unity.omnigent.skillpacks"``.
    :returns: ``(catalog, schema, volume)``, e.g.
        ``("unity", "omnigent", "skillpacks")``.
    :raises ValueError: If it is not exactly three dot-separated, non-empty
        parts.
    """
    parts = storage_location.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            "storage_location must be a three-level volume name "
            f"'<catalog>.<schema>.<volume>', got: {storage_location!r}"
        )
    return parts[0], parts[1], parts[2]


def _validate_key(key: str) -> None:
    """
    Validate an artifact key against traversal attacks.

    Same validation as ``LocalArtifactStore`` / ``S3ArtifactStore`` — reject
    empty keys, ``..`` sequences, backslashes, and absolute paths — so a
    crafted key can't escape the volume storage directory.

    :param key: Forward-slash-separated artifact key, e.g.
        ``"skills/code-review/code-review.tar.gz"``.
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


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    """
    Decode an HTTP response body as a JSON object, narrowing its type.

    ``httpx.Response.json()`` is typed ``Any``; returning it directly from
    a function annotated ``-> dict[str, ...]`` trips mypy's strict
    ``no-any-return``. This helper does the JSON decode and a runtime
    ``isinstance`` narrowing at the boundary, which both satisfies the type
    checker and hardens against a malformed non-object body from the server.

    :param resp: A 2xx :class:`httpx.Response` whose body is expected to be a
        JSON object (UC returns ``VolumeInfo`` / ``CatalogInfo`` / etc.).
    :returns: The decoded body as a ``dict``.
    :raises ValueError: If the body is not a JSON object (list, string,
        number) — a sign the server contract changed or the wrong endpoint
        was hit.
    """
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(
            f"expected a JSON object from {resp.request.method} {resp.request.url}, "
            f"got {type(data).__name__}"
        )
    return data


class UnityCatalogRestClient:
    """
    Thin HTTP client for the Unity Catalog OSS REST API.

    Wraps an :class:`httpx.Client` and exposes just the operations this POC
    needs: resolving a volume, vending temporary volume credentials, and
    creating/reading catalogs, schemas, volumes, and tables. Kept separate
    from the store so the ``skillpack`` CLI can reuse it for the metadata
    table without importing ``ArtifactStore``.

    :param uri: Base server URI (scheme + host + port, no API path), e.g.
        ``"http://localhost:8080"``.
    :param token: Optional bearer token. Local UC OSS runs with auth disabled
        by default, so this is usually ``None``.
    :param client: Optional pre-built :class:`httpx.Client` — used by tests to
        inject an :class:`httpx.MockTransport`. When omitted, a client is
        constructed from *uri* and *token*.
    """

    def __init__(
        self,
        uri: str = _DEFAULT_URI,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """
        Build the REST client.

        :param uri: Base server URI, e.g. ``"http://localhost:8080"``.
        :param token: Optional bearer token, or ``None`` for the auth-disabled
            local default.
        :param client: Optional injected :class:`httpx.Client` (tests).
        """
        _ensure_httpx()
        import httpx

        self._uri = uri.rstrip("/")
        if client is not None:
            self._client = client
        else:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            self._client = httpx.Client(
                base_url=f"{self._uri}{_UC_API_PREFIX}",
                headers=headers,
                timeout=30.0,
            )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> UnityCatalogRestClient:
        """Enter a context manager, returning ``self``."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit the context manager, closing the HTTP client."""
        self.close()

    # ── volumes ──────────────────────────────────────────────

    def get_volume(self, full_name: str) -> dict[str, Any]:
        """
        Fetch a volume's metadata by three-level name.

        :param full_name: Fully-qualified volume name, e.g.
            ``"unity.omnigent.skillpacks"``.
        :returns: The ``VolumeInfo`` JSON — includes ``storage_location``,
            ``volume_id``, ``volume_type``, etc.
        :raises KeyError: If the volume does not exist (HTTP 404).
        :raises httpx.HTTPStatusError: On other non-2xx responses.
        """
        import httpx

        resp = self._client.get(f"/volumes/{full_name}")
        if resp.status_code == httpx.codes.NOT_FOUND:
            raise KeyError(f"volume not found: {full_name}")
        resp.raise_for_status()
        return _json_object(resp)

    def create_volume(
        self,
        catalog: str,
        schema: str,
        name: str,
        storage_location: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """
        Create an EXTERNAL volume backed by an explicit storage path.

        EXTERNAL (rather than MANAGED) is used so the POC controls the exact
        ``storage_location`` and can bind-mount it into the container.

        :param catalog: Parent catalog name, e.g. ``"unity"``.
        :param schema: Parent schema name, e.g. ``"omnigent"``.
        :param name: Volume name, e.g. ``"skillpacks"``.
        :param storage_location: Backing storage URI. For local UC OSS a
            ``file://`` path, e.g. ``"file:///home/unitycatalog/etc/data/skillpacks"``.
        :param comment: Optional free-form description.
        :returns: The created ``VolumeInfo`` JSON.
        :raises httpx.HTTPStatusError: On a non-2xx response.
        """
        body: dict[str, Any] = {
            "catalog_name": catalog,
            "schema_name": schema,
            "name": name,
            "volume_type": "EXTERNAL",
            "storage_location": storage_location,
        }
        if comment is not None:
            body["comment"] = comment
        resp = self._client.post("/volumes", json=body)
        resp.raise_for_status()
        return _json_object(resp)

    def generate_temporary_volume_credentials(
        self,
        volume_id: str,
        operation: str = "READ_VOLUME",
    ) -> dict[str, Any]:
        """
        Vend temporary credentials for a volume's backing storage.

        The UC OSS equivalent of the cloud credential-vending flow. For
        **cloud** volumes the response carries ``aws_temp_credentials`` /
        ``azure_user_delegation_sas`` / ``gcp_oauth_token``. For a **local**
        ``file://`` volume there are no cloud credentials to hand out; UC
        returns the normalized storage ``url`` and this store reads/writes
        that path on the (bind-mounted) filesystem directly. Exposed for
        parity/completeness — the store's happy path resolves the storage
        location via :meth:`get_volume`.

        :param volume_id: The volume's ``volume_id`` (from :meth:`get_volume`).
        :param operation: ``"READ_VOLUME"`` or ``"WRITE_VOLUME"``.
        :returns: The ``TemporaryCredentials`` JSON.
        :raises httpx.HTTPStatusError: On a non-2xx response.
        """
        resp = self._client.post(
            "/temporary-volume-credentials",
            json={"volume_id": volume_id, "operation": operation},
        )
        resp.raise_for_status()
        return _json_object(resp)

    # ── catalogs / schemas ───────────────────────────────────

    def create_catalog(self, name: str, comment: str | None = None) -> dict[str, Any]:
        """
        Create a catalog, tolerating one that already exists.

        :param name: Catalog name, e.g. ``"unity"``.
        :param comment: Optional free-form description.
        :returns: The ``CatalogInfo`` JSON.
        :raises httpx.HTTPStatusError: On a non-2xx response other than a 409.
        """
        return self._create_tolerating_conflict(
            "/catalogs",
            {"name": name, **({"comment": comment} if comment else {})},
            f"/catalogs/{name}",
        )

    def create_schema(self, catalog: str, name: str, comment: str | None = None) -> dict[str, Any]:
        """
        Create a schema, tolerating one that already exists.

        :param catalog: Parent catalog name, e.g. ``"unity"``.
        :param name: Schema name, e.g. ``"omnigent"``.
        :param comment: Optional free-form description.
        :returns: The ``SchemaInfo`` JSON.
        :raises httpx.HTTPStatusError: On a non-2xx response other than a 409.
        """
        return self._create_tolerating_conflict(
            "/schemas",
            {"name": name, "catalog_name": catalog, **({"comment": comment} if comment else {})},
            f"/schemas/{catalog}.{name}",
        )

    def _create_tolerating_conflict(
        self,
        create_path: str,
        body: dict[str, Any],
        get_path: str,
    ) -> dict[str, Any]:
        """
        POST to *create_path*; on 409 conflict, GET the existing object.

        UC OSS returns 409 when a securable already exists. For an idempotent
        bootstrap ("create the catalog if absent") that is a success, so this
        swallows the conflict and returns the existing object's metadata.

        :param create_path: The create endpoint, e.g. ``"/catalogs"``.
        :param body: The JSON request body for creation.
        :param get_path: The endpoint to GET on a 409, e.g. ``"/catalogs/unity"``.
        :returns: The created (or pre-existing) object JSON.
        :raises httpx.HTTPStatusError: On a non-2xx response other than 409.
        """
        import httpx

        resp = self._client.post(create_path, json=body)
        if resp.status_code == httpx.codes.CONFLICT:
            existing = self._client.get(get_path)
            existing.raise_for_status()
            return _json_object(existing)
        resp.raise_for_status()
        return _json_object(resp)


class UnityCatalogOSSArtifactStore(ArtifactStore):
    """
    Stores binary blobs in a Unity Catalog OSS volume.

    Resolves the volume's ``storage_location`` from the UC OSS REST API, then
    reads/writes blob bytes on the filesystem at that location (see the module
    docstring for why bytes don't travel over REST in OSS). Keys are
    forward-slash-separated and joined onto the resolved storage directory::

        <storage_dir>/
            skills/code-review/code-review.tar.gz
            skills/triage/triage.tar.gz

    :param storage_location: Three-level volume name
        ``<catalog>.<schema>.<volume>``, e.g. ``"unity.omnigent.skillpacks"``.
        Read from ``UC_OSS_VOLUME`` when omitted.
    :param uri: UC OSS server base URI, e.g. ``"http://localhost:8080"``.
        Read from ``UC_OSS_URI`` when omitted.
    :param token: Optional bearer token. Read from ``UC_OSS_TOKEN`` when
        omitted (local UC OSS defaults to auth disabled).
    :param local_root: Host filesystem path where the volume's
        ``storage_location`` is accessible — required when the UC server runs
        in a container and the ``file://`` path is only reachable via a bind
        mount. Read from ``UC_OSS_VOLUME_LOCAL_PATH`` when omitted. When
        neither is set, the ``file://`` ``storage_location`` from the REST API
        is used directly (valid when the store runs on the same host as a
        non-containerized UC server).
    :param client: Optional injected :class:`UnityCatalogRestClient` (tests
        inject one backed by an :class:`httpx.MockTransport`).
    """

    def __init__(
        self,
        storage_location: str | None = None,
        uri: str | None = None,
        token: str | None = None,
        local_root: str | None = None,
        client: UnityCatalogRestClient | None = None,
    ) -> None:
        """
        Initialize the UC OSS artifact store.

        Configuration precedence is explicit argument, then environment
        variable, then default — matching how ``s3.py`` reads ambient config.

        :param storage_location: Three-level volume name, or ``None`` to read
            ``UC_OSS_VOLUME``.
        :param uri: Server base URI, or ``None`` to read ``UC_OSS_URI``
            (default ``"http://localhost:8080"``).
        :param token: Bearer token, or ``None`` to read ``UC_OSS_TOKEN``.
        :param local_root: Host path to the volume storage, or ``None`` to read
            ``UC_OSS_VOLUME_LOCAL_PATH``.
        :param client: Optional injected REST client.
        :raises ImportError: If ``httpx`` is not installed.
        :raises ValueError: If no volume name is provided or resolvable, or the
            name is not a valid three-level name.
        """
        _ensure_httpx()
        resolved = storage_location or os.environ.get("UC_OSS_VOLUME")
        if not resolved:
            raise ValueError(
                "storage_location is required: pass it or set UC_OSS_VOLUME to a "
                "three-level name like 'unity.omnigent.skillpacks'"
            )
        _parse_volume_full_name(resolved)  # validate shape early
        super().__init__(resolved)

        self._uri = uri or os.environ.get("UC_OSS_URI") or _DEFAULT_URI
        self._token = token if token is not None else os.environ.get("UC_OSS_TOKEN")
        self._local_root = local_root or os.environ.get("UC_OSS_VOLUME_LOCAL_PATH")
        self._client = (
            client
            if client is not None
            else UnityCatalogRestClient(uri=self._uri, token=self._token)
        )
        # Resolved lazily on first I/O so construction never requires a live
        # server (keeps unit tests and __init__ side-effect-free).
        self._storage_dir: Path | None = None

    def _resolve_storage_dir(self) -> Path:
        """
        Resolve (and cache) the local directory backing the volume.

        Queries the UC OSS REST API for the volume's ``storage_location`` —
        the metadata step that couples the store to a live UC server (the
        local analogue of cloud credential vending). The path is then:

        - ``local_root`` (if configured): used directly as the base directory
          — the host-visible bind mount of the container's ``file://`` path.
        - otherwise: the ``file://`` ``storage_location`` is parsed and its
          filesystem path used directly (non-containerized server, same host).

        :returns: The resolved base :class:`Path`, created if absent.
        :raises KeyError: If the volume does not exist on the server.
        :raises ValueError: If no ``local_root`` is set and the volume's
            ``storage_location`` is not a ``file://`` URI (e.g. a cloud
            ``s3://`` volume, which this filesystem-backed store cannot serve
            without vended cloud credentials).
        """
        if self._storage_dir is not None:
            return self._storage_dir

        info = self._client.get_volume(self._storage_location)
        if self._local_root:
            base = Path(self._local_root)
        else:
            raw = info.get("storage_location")
            if not isinstance(raw, str):
                raise ValueError(f"volume {self._storage_location!r} has no storage_location")
            parsed = urlparse(raw)
            if parsed.scheme not in ("file", ""):
                raise ValueError(
                    f"volume storage_location {raw!r} is not a file:// path; set "
                    "local_root / UC_OSS_VOLUME_LOCAL_PATH to the host mount of the "
                    "volume storage (cloud volumes need vended credentials, which "
                    "this filesystem-backed POC store does not implement)"
                )
            base = Path(unquote(parsed.path))

        base.mkdir(parents=True, exist_ok=True)
        self._storage_dir = base
        return base

    def _resolve(self, key: str) -> Path:
        """
        Map *key* to an absolute filesystem path under the volume root.

        :param key: Forward-slash-separated artifact key.
        :returns: The resolved absolute :class:`Path`.
        :raises ValueError: If the key is invalid or resolves outside the
            volume storage directory.
        """
        _validate_key(key)
        base = self._resolve_storage_dir()
        resolved = (base / Path(*PurePosixPath(key).parts)).resolve()
        if not resolved.is_relative_to(base.resolve()):
            raise ValueError(f"artifact key escapes volume storage: {key!r}")
        return resolved

    # ── ArtifactStore interface ──────────────────────────────

    def put(self, key: str, data: bytes) -> None:
        """
        Write bytes to the volume storage under *key*. Overwrites if present.

        :param key: Forward-slash-separated artifact key.
        :param data: Raw bytes to store.
        """
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        """
        Read bytes from the volume storage.

        Only regular files are artifacts: a key that resolves to a directory
        (e.g. a prefix like ``"skills"``) raises :class:`KeyError`, not an
        ``IsADirectoryError`` — matching object-store semantics where a prefix
        is not itself a retrievable object.

        :param key: Forward-slash-separated artifact key.
        :returns: The raw bytes of the stored blob.
        :raises KeyError: If no regular file exists for the key.
        """
        path = self._resolve(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def delete(self, key: str) -> None:
        """
        Remove a blob from the volume storage. No-op if absent.

        Only a regular file is unlinked; a key resolving to a directory (a
        prefix) is a no-op, never a recursive delete.

        :param key: Forward-slash-separated artifact key.
        """
        path = self._resolve(key)
        if path.is_file():
            path.unlink()

    def exists(self, key: str) -> bool:
        """
        Check whether a blob exists for *key*.

        Returns ``True`` only for a regular file; a directory key (a prefix)
        is ``False``, so ``exists`` and ``get`` agree on what is an artifact.

        :param key: Forward-slash-separated artifact key.
        :returns: ``True`` if a regular file exists for the key, else ``False``.
        """
        return self._resolve(key).is_file()

    # ── beyond the ABC ───────────────────────────────────────

    def list(self, prefix: str = "") -> list[str]:
        """
        List keys of all blobs whose key starts with *prefix*.

        Not part of the :class:`ArtifactStore` ABC (which is
        put/get/delete/exists only) — this backend adds it because the
        ``skillpack`` CLI needs to enumerate stored packs, and a local UC OSS
        volume can be walked cheaply on the filesystem. Kept concrete-only to
        avoid forcing a ``list`` implementation onto the other backends.

        :param prefix: Key prefix to filter by, e.g. ``"skills/"``. The
            empty-string default returns every key.
        :returns: Sorted list of matching forward-slash keys, e.g.
            ``["skills/code-review/code-review.tar.gz"]``.
        """
        base = self._resolve_storage_dir()
        keys: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            if rel.startswith(prefix):
                keys.append(rel)
        return sorted(keys)
