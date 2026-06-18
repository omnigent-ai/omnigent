"""Integration tests for S3ArtifactStore against a mocked S3/R2 backend.

The mock is `moto` (the standard S3-compatible mock library) rather than a
hand-written stub: R2 implements the S3 API, so moto faithfully exercises the
real boto3 client paths the store uses against Cloudflare R2.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from omnigent.stores.artifact_store.s3 import S3ArtifactStore, _make_client

_BUCKET = "omnigent-test-artifacts"


@pytest.fixture()
def s3_store():
    """An S3ArtifactStore backed by a moto-mocked bucket, prefixed."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield S3ArtifactStore(f"s3://{_BUCKET}/artifacts", client=client)


@pytest.fixture()
def s3_store_no_prefix():
    """An S3ArtifactStore with a bare bucket (no prefix)."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield S3ArtifactStore(f"s3://{_BUCKET}", client=client)


def test_put_and_get(s3_store):
    """Round-trip: a stored blob comes back byte-identical."""
    s3_store.put("abc123", b"hello world")
    assert s3_store.get("abc123") == b"hello world"


def test_get_missing_raises_key_error(s3_store):
    """A missing key must raise KeyError (not botocore's NoSuchKey) so callers
    like the agent-bundle loader can catch a stable exception type."""
    with pytest.raises(KeyError, match="no-such-key"):
        s3_store.get("no-such-key")


def test_nested_key_put_get(s3_store):
    """Forward-slash keys map to nested object keys, like agent bundles do."""
    s3_store.put("agents/agent_abc/bundle.tar.gz", b"bundle-data")
    assert s3_store.get("agents/agent_abc/bundle.tar.gz") == b"bundle-data"


def test_overwrite(s3_store):
    """put() overwrites an existing key in place."""
    s3_store.put("k", b"first")
    s3_store.put("k", b"second")
    assert s3_store.get("k") == b"second"


def test_exists(s3_store):
    """exists() is True only after a put, via HEAD (no download)."""
    assert s3_store.exists("k") is False
    s3_store.put("k", b"x")
    assert s3_store.exists("k") is True


def test_delete(s3_store):
    """delete() removes the object; a later get raises KeyError."""
    s3_store.put("k", b"x")
    s3_store.delete("k")
    assert s3_store.exists("k") is False
    with pytest.raises(KeyError):
        s3_store.get("k")


def test_delete_missing_is_noop(s3_store):
    """Deleting a missing key is a no-op (S3 DeleteObject is idempotent)."""
    s3_store.delete("never-existed")  # must not raise


def test_prefix_isolation(s3_store):
    """Keys land under the configured prefix — verify via the raw object key."""
    s3_store.put("k", b"v")
    listing = s3_store._client.list_objects_v2(Bucket=_BUCKET)
    keys = [o["Key"] for o in listing.get("Contents", [])]
    assert keys == ["artifacts/k"]


def test_no_prefix(s3_store_no_prefix):
    """A bucket-only URI stores objects at the bucket root."""
    s3_store_no_prefix.put("k", b"v")
    listing = s3_store_no_prefix._client.list_objects_v2(Bucket=_BUCKET)
    keys = [o["Key"] for o in listing.get("Contents", [])]
    assert keys == ["k"]


@pytest.mark.parametrize(
    "bad_key",
    ["", "..", "../etc/passwd", "foo\\bar", "a/../b", "/absolute/path", "C:/windows"],
)
def test_rejects_invalid_keys(s3_store, bad_key):
    """Traversal-style keys are rejected before any S3 call, so a crafted key
    can't escape the configured prefix."""
    with pytest.raises(ValueError, match="invalid artifact key"):
        s3_store.put(bad_key, b"x")


@pytest.mark.parametrize(
    "uri",
    ["http://not-s3/bucket", "s3://", "dbfs:/Volumes/x"],
)
def test_rejects_bad_storage_location(uri):
    """A non-s3:// (or bucket-less) storage_location fails fast at construction."""
    with mock_aws():
        with pytest.raises(ValueError):
            S3ArtifactStore(uri, client=boto3.client("s3", region_name="us-east-1"))


def test_make_client_uses_r2_endpoint(monkeypatch):
    """_make_client wires AWS_ENDPOINT_URL_S3 through to boto3 — this is how the
    same backend points at Cloudflare R2 (or MinIO) instead of AWS."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    client = _make_client()
    assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"


def test_non_notfound_error_propagates(s3_store, monkeypatch):
    """A non-404 S3 error (e.g. AccessDenied) must NOT be swallowed as a missing
    key — get/exists re-raise it so real failures surface."""

    def _boom(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "GetObject",
        )

    monkeypatch.setattr(s3_store._client, "get_object", _boom)
    with pytest.raises(ClientError):
        s3_store.get("k")
