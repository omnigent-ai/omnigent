"""Tests for UnityCatalogOSSArtifactStore against a mocked UC OSS REST API.

The REST API is mocked with a real :class:`httpx.MockTransport` (not a
MagicMock) so the store's actual HTTP request shapes are exercised; blob bytes
round-trip through a real temp-filesystem volume storage directory. No Docker
or live server is required — the Docker path is exercised manually via the
POC README / demo.sh.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.artifact_store.unitycatalog_oss import (
    UnityCatalogOSSArtifactStore,
    UnityCatalogRestClient,
    _json_object,
    _parse_volume_full_name,
    _validate_key,
)

_VOLUME = "unity.omnigent.skillpacks"


# ── _parse_volume_full_name ─────────────────────────────────


def test_parse_volume_full_name_basic():
    """A three-level name splits into (catalog, schema, volume)."""
    assert _parse_volume_full_name("unity.omnigent.skillpacks") == (
        "unity",
        "omnigent",
        "skillpacks",
    )


@pytest.mark.parametrize(
    "bad",
    ["", "unity", "unity.omnigent", "unity.omnigent.skillpacks.extra", "unity..vol", ".a.b"],
)
def test_parse_volume_full_name_rejects_bad_shapes(bad):
    """Names without exactly three non-empty parts fail fast at construction,
    not with a confusing downstream 404."""
    with pytest.raises(ValueError, match="three-level"):
        _parse_volume_full_name(bad)


# ── _validate_key (same contract as s3/local) ───────────────


@pytest.mark.parametrize(
    "bad_key",
    ["", "..", "../etc/passwd", "foo\\bar", "a/../b", "/absolute/path", "C:/windows"],
)
def test_validate_key_rejects_traversal(bad_key):
    """Traversal-style keys are rejected so a crafted key can't escape the
    volume storage directory."""
    with pytest.raises(ValueError, match="invalid artifact key"):
        _validate_key(bad_key)


def test_validate_key_accepts_valid_keys():
    """Normal forward-slash keys pass validation."""
    _validate_key("skills/code-review/code-review.tar.gz")
    _validate_key("_manifest/skills.json")
    _validate_key("simple")


# ── REST mock plumbing ──────────────────────────────────────


def _make_store(
    tmp_path: Path,
    *,
    volume: str = _VOLUME,
    storage_location: str | None = None,
    local_root: str | None = None,
    volume_present: bool = True,
) -> UnityCatalogOSSArtifactStore:
    """Build a store whose REST client is backed by an httpx.MockTransport.

    The mock answers ``GET /volumes/<full_name>`` with a VolumeInfo whose
    ``storage_location`` is *storage_location* (defaulting to a ``file://`` URI
    under *tmp_path*), or 404 when *volume_present* is False.
    """
    if storage_location is None:
        storage_location = (tmp_path / "vol").as_uri()

    def handler(request: httpx.Request) -> httpx.Response:
        expected = f"/api/2.1/unity-catalog/volumes/{volume}"
        if request.method == "GET" and request.url.path == expected:
            if not volume_present:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(
                200,
                json={
                    "catalog_name": volume.split(".")[0],
                    "schema_name": volume.split(".")[1],
                    "name": volume.split(".")[2],
                    "volume_id": "vol-test-123",
                    "volume_type": "EXTERNAL",
                    "storage_location": storage_location,
                    "full_name": volume,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(
        base_url="http://uc.test/api/2.1/unity-catalog",
        transport=httpx.MockTransport(handler),
    )
    rest = UnityCatalogRestClient(uri="http://uc.test", client=client)
    return UnityCatalogOSSArtifactStore(
        storage_location=volume, local_root=local_root, client=rest
    )


@pytest.fixture()
def store(tmp_path):
    """A store whose volume storage lives at ``tmp_path/vol`` (file:// URI)."""
    return _make_store(tmp_path)


# ── construction / config ───────────────────────────────────


def test_requires_volume(monkeypatch):
    """Construction fails loud when no volume is given and none in env, rather
    than failing confusingly on first I/O."""
    monkeypatch.delenv("UC_OSS_VOLUME", raising=False)
    with pytest.raises(ValueError, match="storage_location is required"):
        UnityCatalogOSSArtifactStore()


def test_volume_from_env(monkeypatch, tmp_path):
    """UC_OSS_VOLUME supplies the volume when the arg is omitted (the config
    path the CLI/demo use)."""
    monkeypatch.setenv("UC_OSS_VOLUME", _VOLUME)
    monkeypatch.setenv("UC_OSS_VOLUME_LOCAL_PATH", str(tmp_path / "vol"))
    store = UnityCatalogOSSArtifactStore()
    assert store.storage_location == _VOLUME


def test_is_artifact_store(store):
    """The store is a proper ArtifactStore subclass so it drops in wherever an
    ArtifactStore is expected."""
    assert isinstance(store, ArtifactStore)
    assert store.storage_location == _VOLUME


# ── put / get / delete / exists ─────────────────────────────


def test_put_and_get_round_trip(store):
    """Bytes written under a key read back identically."""
    store.put("skills/foo/foo.tar.gz", b"hello world")
    assert store.get("skills/foo/foo.tar.gz") == b"hello world"


def test_put_writes_to_resolved_storage_location(store, tmp_path):
    """The blob lands at the file:// storage_location the REST API vends —
    proving the store honors the volume's real location rather than writing
    somewhere else."""
    store.put("skills/foo/foo.tar.gz", b"payload")
    on_disk = tmp_path / "vol" / "skills" / "foo" / "foo.tar.gz"
    assert on_disk.read_bytes() == b"payload"


def test_get_missing_raises_key_error(store):
    """A missing key raises KeyError (interface contract) so callers can catch
    a stable exception type."""
    with pytest.raises(KeyError, match="missing"):
        store.get("missing")


def test_delete_existing_and_missing(store):
    """delete removes an existing blob and is a no-op for an absent one."""
    store.put("k", b"data")
    store.delete("k")
    assert not store.exists("k")
    store.delete("k")  # no-op, must not raise


def test_exists_true_false(store):
    """exists reflects presence."""
    assert not store.exists("absent")
    store.put("present", b"x")
    assert store.exists("present")


def test_put_overwrites(store):
    """Re-pushing the same key overwrites the prior blob."""
    store.put("k", b"first")
    store.put("k", b"second")
    assert store.get("k") == b"second"


def test_directory_prefix_is_not_an_artifact(store):
    """A key that resolves to a directory (a prefix) is not an artifact:
    exists() is False and get() raises KeyError, not IsADirectoryError —
    matching object-store semantics."""
    store.put("skills/foo/foo.tar.gz", b"x")
    # "skills" and "skills/foo" are directories on disk, not stored blobs.
    assert store.exists("skills") is False
    assert store.exists("skills/foo") is False
    with pytest.raises(KeyError):
        store.get("skills")
    with pytest.raises(KeyError):
        store.get("skills/foo")
    # delete on a directory key is a no-op (never a recursive delete).
    store.delete("skills")
    assert store.exists("skills/foo/foo.tar.gz") is True


# ── list (concrete-only, not on the ABC) ────────────────────


def test_list_empty(store):
    """Listing an empty volume returns []."""
    assert store.list() == []


def test_list_returns_all_keys_sorted(store):
    """list() returns every blob key, sorted, as forward-slash paths."""
    store.put("skills/b/b.tar.gz", b"1")
    store.put("skills/a/a.tar.gz", b"2")
    store.put("_manifest/skills.json", b"[]")
    assert store.list() == [
        "_manifest/skills.json",
        "skills/a/a.tar.gz",
        "skills/b/b.tar.gz",
    ]


def test_list_prefix_filters(store):
    """list(prefix) returns only keys starting with prefix, so the manifest
    blob doesn't leak into skill listings."""
    store.put("skills/a/a.tar.gz", b"1")
    store.put("_manifest/skills.json", b"[]")
    assert store.list("skills/") == ["skills/a/a.tar.gz"]


def test_list_not_on_abc():
    """list is deliberately NOT on the ArtifactStore ABC — it's a concrete-only
    extension so the sibling backends aren't forced to implement it."""
    assert not hasattr(ArtifactStore, "list")


# ── storage-location resolution ─────────────────────────────


def test_local_root_overrides_reported_storage_location(tmp_path):
    """local_root is used verbatim, ignoring the REST storage_location — the
    containerized case where the file:// path is only valid inside the
    container and the host reaches the bytes via a bind mount."""
    host_mount = tmp_path / "host_mount"
    store = _make_store(
        tmp_path,
        storage_location="file:///home/unitycatalog/etc/data/skillpacks",
        local_root=str(host_mount),
    )
    store.put("skills/x/x.tar.gz", b"bytes")
    assert (host_mount / "skills" / "x" / "x.tar.gz").read_bytes() == b"bytes"


def test_missing_volume_raises_key_error(tmp_path):
    """A first I/O against a non-existent volume surfaces KeyError, not an
    opaque HTTP error."""
    store = _make_store(tmp_path, volume_present=False)
    with pytest.raises(KeyError, match="volume not found"):
        store.put("skills/x/x.tar.gz", b"bytes")


def test_non_file_storage_location_requires_local_root(tmp_path):
    """A cloud (non-file://) volume without local_root fails loud rather than
    silently accepting an s3:// volume this filesystem-backed store can't
    serve."""
    store = _make_store(tmp_path, storage_location="s3://bucket/skillpacks")
    with pytest.raises(ValueError, match="not a file:// path"):
        store.exists("anything")


def test_key_escaping_storage_dir_rejected(store):
    """A key that resolves outside the storage dir is rejected (belt and
    suspenders on top of _validate_key)."""
    with pytest.raises(ValueError, match=r"invalid artifact key|escapes volume"):
        store.put("../outside.tar.gz", b"x")


# ── REST client bootstrap helpers ───────────────────────────


def test_create_catalog_tolerates_conflict():
    """create_catalog swallows a 409 and returns the existing object, so
    re-running bootstrap (or a second push) stays idempotent."""
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path.endswith("/catalogs"):
            return httpx.Response(409, json={"message": "already exists"})
        if request.method == "GET" and request.url.path.endswith("/catalogs/unity"):
            return httpx.Response(200, json={"name": "unity", "comment": "existing"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(
        base_url="http://uc.test/api/2.1/unity-catalog",
        transport=httpx.MockTransport(handler),
    )
    rest = UnityCatalogRestClient(uri="http://uc.test", client=client)
    result = rest.create_catalog("unity")
    assert result["comment"] == "existing"
    assert "POST /api/2.1/unity-catalog/catalogs" in requests_seen
    assert "GET /api/2.1/unity-catalog/catalogs/unity" in requests_seen


def test_get_volume_404_raises_key_error():
    """get_volume maps a 404 to KeyError so absent-volume detection doesn't
    leak the raw HTTPStatusError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.Client(
        base_url="http://uc.test/api/2.1/unity-catalog",
        transport=httpx.MockTransport(handler),
    )
    rest = UnityCatalogRestClient(uri="http://uc.test", client=client)
    with pytest.raises(KeyError, match="volume not found"):
        rest.get_volume("unity.omnigent.nope")


# ── _json_object boundary narrowing ─────────────────────────


def test_json_object_returns_dict_body():
    """_json_object returns a JSON object body as a plain dict."""
    resp = httpx.Response(
        200,
        json={"name": "unity", "volume_id": "v1"},
        request=httpx.Request("GET", "http://uc.test/volumes/unity.omnigent.x"),
    )
    assert _json_object(resp) == {"name": "unity", "volume_id": "v1"}


def test_json_object_rejects_non_object_body():
    """_json_object raises ValueError on a non-object body — the runtime
    narrowing that keeps the REST helpers' `-> dict` return honest under mypy
    strict and fails loud on a malformed server response."""
    resp = httpx.Response(
        200,
        json=["not", "an", "object"],
        request=httpx.Request("GET", "http://uc.test/volumes/unity.omnigent.x"),
    )
    with pytest.raises(ValueError, match="expected a JSON object"):
        _json_object(resp)
