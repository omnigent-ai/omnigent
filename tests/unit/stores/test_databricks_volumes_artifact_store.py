"""Tests for DatabricksVolumesArtifactStore.

Tests key validation and URI parsing without requiring a Databricks
workspace. SDK calls are tested with stubs that verify the correct
paths are constructed.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from omnigent.stores.artifact_store.databricks_volumes import (
    DatabricksVolumesArtifactStore,
    _parse_volume_root,
    _validate_key,
)

# ── _parse_volume_root ──────────────────────────────────────


def test_parse_volume_root_basic() -> None:
    """
    Standard URI with catalog/schema/volume extracts the path.

    **What breaks if wrong**: store constructs wrong file paths,
    causing all operations to fail with NotFound.
    """
    result = _parse_volume_root("dbfs:/Volumes/my_cat/my_schema/my_vol")
    assert result == "/Volumes/my_cat/my_schema/my_vol"


def test_parse_volume_root_with_prefix() -> None:
    """
    URI with a subdirectory prefix preserves the full path.

    **What breaks if wrong**: prefix is stripped, artifacts land
    in the volume root instead of the intended subdirectory.
    """
    result = _parse_volume_root("dbfs:/Volumes/cat/schema/vol/omnigent/artifacts")
    assert result == "/Volumes/cat/schema/vol/omnigent/artifacts"


def test_parse_volume_root_rejects_non_dbfs() -> None:
    """
    URIs without the ``dbfs:/Volumes/`` prefix are rejected.

    **What breaks if wrong**: local paths or other schemes silently
    accepted, leading to SDK API errors or local filesystem writes.
    """
    with pytest.raises(ValueError, match="must start with"):
        _parse_volume_root("/Volumes/cat/schema/vol")

    with pytest.raises(ValueError, match="must start with"):
        _parse_volume_root("s3://bucket/path")

    with pytest.raises(ValueError, match="must start with"):
        _parse_volume_root("dbfs:/some/other/path")


# ── _validate_key ───────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "..",
        "../etc/passwd",
        "foo\\bar",
        "a/../b",
        "valid/../../escape",
        "/absolute/path",
        "C:/windows/drive",
    ],
)
def test_validate_key_rejects_traversal(bad_key: str) -> None:
    """
    Same traversal protection as LocalArtifactStore.

    **What breaks if wrong**: attacker can read/write arbitrary
    files on the UC Volume outside the intended prefix.
    """
    with pytest.raises(ValueError, match="invalid artifact key"):
        _validate_key(bad_key)


def test_validate_key_accepts_valid_keys() -> None:
    """
    Normal forward-slash keys pass validation.

    **What breaks if wrong**: legitimate artifact keys rejected,
    blocking all store operations.
    """
    # These should not raise
    _validate_key("agents/ag_abc123/bundle.tar.gz")
    _validate_key("executor_storage/conv_123/agent.tar.gz")
    _validate_key("simple_key")


# ── _resolve path construction ──────────────────────────────


def test_resolve_constructs_full_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_resolve`` joins root + key into a full volume path.

    **What breaks if wrong**: SDK receives wrong file path,
    operations target wrong location.
    """
    monkeypatch.setattr(
        "omnigent.stores.artifact_store.databricks_volumes._ensure_databricks_sdk",
        lambda: None,
    )
    # Patch the SDK import inside __init__
    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient",
        lambda: _StubWorkspaceClient(),
    )

    store = DatabricksVolumesArtifactStore("dbfs:/Volumes/cat/schema/vol/prefix")
    path = store._resolve("agents/ag_1/bundle.tar.gz")
    assert path == "/Volumes/cat/schema/vol/prefix/agents/ag_1/bundle.tar.gz"


# ── SDK operation stubs ──────────────────────────────────────


class _StubFilesAPI:
    """
    Stub for ``WorkspaceClient.files`` that records calls.

    :param stored: Dict mapping path → bytes for simulated storage.
    """

    def __init__(self, stored: dict[str, bytes] | None = None) -> None:
        self._stored: dict[str, bytes] = stored or {}

    def upload(
        self,
        path: str,
        contents: io.BytesIO,
        overwrite: bool = False,
    ) -> None:
        """Record an upload."""
        self._stored[path] = contents.read()

    def download(self, path: str) -> Any:
        """Return stored bytes or raise NotFound."""
        from databricks.sdk.errors import NotFound

        if path not in self._stored:
            raise NotFound(f"File not found: {path}")

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self.contents = io.BytesIO(data)

        return _Resp(self._stored[path])

    def delete(self, path: str) -> None:
        """Delete from stored or raise NotFound."""
        from databricks.sdk.errors import NotFound

        if path not in self._stored:
            raise NotFound(f"File not found: {path}")
        del self._stored[path]

    def get_metadata(self, path: str) -> Any:
        """Check existence or raise NotFound."""
        from databricks.sdk.errors import NotFound

        if path not in self._stored:
            raise NotFound(f"File not found: {path}")
        return {}


class _StubWorkspaceClient:
    """
    Stub ``WorkspaceClient`` with a ``_StubFilesAPI``.

    :param files: The stub files API instance.
    """

    def __init__(self, files_api: _StubFilesAPI | None = None) -> None:
        self.files = files_api or _StubFilesAPI()


@pytest.fixture()
def stub_store(monkeypatch: pytest.MonkeyPatch) -> DatabricksVolumesArtifactStore:
    """
    Create a ``DatabricksVolumesArtifactStore`` with stubbed SDK.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: A store backed by in-memory stubs.
    """
    monkeypatch.setattr(
        "omnigent.stores.artifact_store.databricks_volumes._ensure_databricks_sdk",
        lambda: None,
    )
    files_api = _StubFilesAPI()
    stub_client = _StubWorkspaceClient(files_api)
    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient",
        lambda: stub_client,
    )

    return DatabricksVolumesArtifactStore("dbfs:/Volumes/cat/schema/vol")


def test_put_and_get_round_trip(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    Basic put/get round-trip through the stub SDK.

    **What breaks if wrong**: data corruption or path mismatch
    between put and get.
    """
    stub_store.put("test/key", b"hello world")
    assert stub_store.get("test/key") == b"hello world"


def test_get_missing_raises_key_error(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    Missing key raises ``KeyError``, not SDK's ``NotFound``.

    **What breaks if wrong**: callers get unexpected exception type,
    breaking error handling throughout the codebase.
    """
    with pytest.raises(KeyError, match="missing"):
        stub_store.get("missing")


def test_delete_existing(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    Delete removes the blob.

    **What breaks if wrong**: stale artifacts persist, wasting
    storage and potentially causing conflicts.
    """
    stub_store.put("to-delete", b"data")
    stub_store.delete("to-delete")
    assert not stub_store.exists("to-delete")


def test_delete_missing_is_noop(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    Deleting a non-existent key is a no-op (per interface contract).

    **What breaks if wrong**: ``NotFound`` exception propagates to
    caller, breaking cleanup flows.
    """
    stub_store.delete("nonexistent")


def test_exists_true(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    ``exists`` returns True for stored blobs.

    **What breaks if wrong**: restore logic skips existing artifacts.
    """
    stub_store.put("present", b"x")
    assert stub_store.exists("present")


def test_exists_false(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    ``exists`` returns False for missing blobs.

    **What breaks if wrong**: restore logic skips artifact store
    fetch, using stale or empty local state.
    """
    assert not stub_store.exists("absent")


def test_put_overwrites(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    Second put to the same key overwrites the first.

    **What breaks if wrong**: stale data persists, agent bundles
    don't update on re-upload.
    """
    stub_store.put("k", b"first")
    stub_store.put("k", b"second")
    assert stub_store.get("k") == b"second"


def test_nested_key(stub_store: DatabricksVolumesArtifactStore) -> None:
    """
    Forward-slash keys work for nested paths.

    **What breaks if wrong**: agent bundles and executor storage
    snapshots (which use nested keys) fail.
    """
    stub_store.put("agents/ag_1/bundle.tar.gz", b"bundle")
    assert stub_store.get("agents/ag_1/bundle.tar.gz") == b"bundle"


# ── workspace_client injection ───────────────────────────────


def test_injected_workspace_client_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A ``workspace_client`` passed to the constructor is used as-is, and the
    ambient-credentials ``WorkspaceClient`` is never built.

    **What breaks if wrong**: a caller that injects a pre-authenticated /
    scoped client silently gets an ambient-credentials client instead, so UC
    Volume operations run under the wrong identity.
    """
    monkeypatch.setattr(
        "omnigent.stores.artifact_store.databricks_volumes._ensure_databricks_sdk",
        lambda: None,
    )

    def _no_ambient() -> None:
        raise AssertionError("ambient WorkspaceClient must not be built when a client is injected")

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _no_ambient)

    injected = _StubWorkspaceClient(_StubFilesAPI())
    store = DatabricksVolumesArtifactStore(
        "dbfs:/Volumes/cat/schema/vol", workspace_client=injected
    )

    assert store._client is injected
    # Operations route through the injected client (round-trips its files API).
    store.put("agents/ag_1/bundle.tar.gz", b"payload")
    assert store.get("agents/ag_1/bundle.tar.gz") == b"payload"


def test_ambient_workspace_client_built_when_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When ``workspace_client`` is omitted, the store builds one from ambient
    credentials (the pre-existing default).

    **What breaks if wrong**: the default no-injection path regresses,
    breaking every existing call site that relies on ambient credentials.
    """
    monkeypatch.setattr(
        "omnigent.stores.artifact_store.databricks_volumes._ensure_databricks_sdk",
        lambda: None,
    )

    ambient = _StubWorkspaceClient()
    built: list[_StubWorkspaceClient] = []

    def _factory() -> _StubWorkspaceClient:
        built.append(ambient)
        return ambient

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _factory)

    store = DatabricksVolumesArtifactStore("dbfs:/Volumes/cat/schema/vol")

    assert store._client is ambient
    assert len(built) == 1  # built exactly once, from the ambient path
