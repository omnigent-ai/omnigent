"""Tests for HostCloneAndBundleFrame / HostCloneAndBundleResultFrame."""

from __future__ import annotations

import base64
import json

import pytest

from omnigent.host.frames import (
    HostCloneAndBundleFrame,
    HostCloneAndBundleResultFrame,
    decode_host_frame,
    encode_host_frame,
)

# ── Request frame round-trips ─────────────────────────────


def test_clone_and_bundle_frame_round_trip_full() -> None:
    """
    Verify HostCloneAndBundleFrame with all optional fields survives
    encode → decode.
    """
    original = HostCloneAndBundleFrame(
        request_id="req_clone_1",
        git_url="https://github.com/owner/repo.git",
        git_ref="main",
        git_subpath="packages/core",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCloneAndBundleFrame)
    assert decoded.request_id == "req_clone_1"
    assert decoded.git_url == "https://github.com/owner/repo.git"
    assert decoded.git_ref == "main"
    assert decoded.git_subpath == "packages/core"


def test_clone_and_bundle_frame_round_trip_minimal() -> None:
    """
    Verify HostCloneAndBundleFrame with no optional fields defaults
    to None for git_ref and git_subpath.
    """
    original = HostCloneAndBundleFrame(
        request_id="req_clone_2",
        git_url="https://github.com/owner/repo.git",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCloneAndBundleFrame)
    assert decoded.request_id == "req_clone_2"
    assert decoded.git_url == "https://github.com/owner/repo.git"
    assert decoded.git_ref is None
    assert decoded.git_subpath is None


def test_clone_and_bundle_frame_round_trip_ref_only() -> None:
    """
    Verify HostCloneAndBundleFrame with git_ref but no subpath.
    """
    original = HostCloneAndBundleFrame(
        request_id="req_clone_3",
        git_url="https://github.com/owner/repo.git",
        git_ref="v1.2.3",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCloneAndBundleFrame)
    assert decoded.git_ref == "v1.2.3"
    assert decoded.git_subpath is None


# ── Result frame round-trips ──────────────────────────────


def test_clone_and_bundle_result_frame_ok_round_trip() -> None:
    """
    Verify HostCloneAndBundleResultFrame (success) survives
    encode → decode, including a realistic base64 bundle payload.
    """
    raw_bytes = b"hello tarball"
    b64_payload = base64.b64encode(raw_bytes).decode()

    original = HostCloneAndBundleResultFrame(
        request_id="req_clone_1",
        status="ok",
        bundle_b64=b64_payload,
        commit_sha="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        resolved_ref="main",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCloneAndBundleResultFrame)
    assert decoded.request_id == "req_clone_1"
    assert decoded.status == "ok"
    assert decoded.bundle_b64 == b64_payload
    # Verify the base64 payload survives exactly
    assert base64.b64decode(decoded.bundle_b64) == raw_bytes
    assert decoded.commit_sha == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    assert decoded.resolved_ref == "main"
    assert decoded.error is None


def test_clone_and_bundle_result_frame_failed_round_trip() -> None:
    """
    Verify HostCloneAndBundleResultFrame (failure) round-trips
    correctly with bundle_b64 and other payload fields as None.
    """
    original = HostCloneAndBundleResultFrame(
        request_id="req_clone_2",
        status="failed",
        error="repository not found",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCloneAndBundleResultFrame)
    assert decoded.request_id == "req_clone_2"
    assert decoded.status == "failed"
    assert decoded.bundle_b64 is None
    assert decoded.commit_sha is None
    assert decoded.resolved_ref is None
    assert decoded.error == "repository not found"


# ── Decode error cases ────────────────────────────────────


def test_clone_and_bundle_frame_missing_request_id() -> None:
    """
    Decode rejects a clone-and-bundle request missing request_id.
    """
    raw = json.dumps(
        {
            "kind": "host.clone_and_bundle",
            "git_url": "https://github.com/owner/repo.git",
        }
    )
    with pytest.raises(ValueError, match="request_id"):
        decode_host_frame(raw)


def test_clone_and_bundle_frame_missing_git_url() -> None:
    """
    Decode rejects a clone-and-bundle request missing git_url.
    """
    raw = json.dumps(
        {
            "kind": "host.clone_and_bundle",
            "request_id": "req_clone_1",
        }
    )
    with pytest.raises(ValueError, match="git_url"):
        decode_host_frame(raw)


def test_clone_and_bundle_frame_wrong_type_request_id() -> None:
    """
    Decode rejects a clone-and-bundle request with a non-string request_id.
    """
    raw = json.dumps(
        {
            "kind": "host.clone_and_bundle",
            "request_id": 42,
            "git_url": "https://github.com/owner/repo.git",
        }
    )
    with pytest.raises(ValueError, match="request_id"):
        decode_host_frame(raw)


def test_clone_and_bundle_frame_wrong_type_git_ref() -> None:
    """
    Decode rejects a clone-and-bundle request where git_ref is not a string/null.
    """
    raw = json.dumps(
        {
            "kind": "host.clone_and_bundle",
            "request_id": "req_clone_1",
            "git_url": "https://github.com/owner/repo.git",
            "git_ref": 123,
        }
    )
    with pytest.raises(ValueError, match="git_ref"):
        decode_host_frame(raw)


def test_clone_and_bundle_result_frame_missing_request_id() -> None:
    """
    Decode rejects a clone-and-bundle result missing request_id.
    """
    raw = json.dumps(
        {
            "kind": "host.clone_and_bundle_result",
            "status": "ok",
        }
    )
    with pytest.raises(ValueError, match="request_id"):
        decode_host_frame(raw)


def test_clone_and_bundle_result_frame_missing_status() -> None:
    """
    Decode rejects a clone-and-bundle result missing status.
    """
    raw = json.dumps(
        {
            "kind": "host.clone_and_bundle_result",
            "request_id": "req_clone_1",
        }
    )
    with pytest.raises(ValueError, match="status"):
        decode_host_frame(raw)


def test_clone_and_bundle_result_frame_wrong_type_bundle_b64() -> None:
    """
    Decode rejects a clone-and-bundle result where bundle_b64 is not a string/null.
    """
    raw = json.dumps(
        {
            "kind": "host.clone_and_bundle_result",
            "request_id": "req_clone_1",
            "status": "ok",
            "bundle_b64": 999,
        }
    )
    with pytest.raises(ValueError, match="bundle_b64"):
        decode_host_frame(raw)
