"""
Unit tests for deferred action models and deterministic manifest hashing.
"""

import pytest

from omnigent.runtime.deferred.hashing import compute_manifest_hash
from omnigent.runtime.deferred.models import DeferredManifest


def test_compute_manifest_hash_deterministic():
    """Verify identical manifest payloads produce identical SHA256 hashes regardless of key order."""
    manifest1 = DeferredManifest(
        tool="sys_os_edit",
        arguments={"path": "app.py", "content": "print('hello')", "mode": "overwrite"},
        base_hash="sha256_base_12345",
        session_id="conv_abc123",
        target="app.py",
    )
    manifest2 = DeferredManifest(
        tool="sys_os_edit",
        arguments={"mode": "overwrite", "content": "print('hello')", "path": "app.py"},
        base_hash="sha256_base_12345",
        session_id="conv_abc123",
        target="app.py",
    )

    hash1 = compute_manifest_hash(manifest1)
    hash2 = compute_manifest_hash(manifest2)

    assert hash1 == hash2
    assert len(hash1) == 64


def test_compute_manifest_hash_tamper_detection():
    """Verify altering any field in manifest changes the computed hash."""
    base_manifest = DeferredManifest(
        tool="sys_os_edit",
        arguments={"path": "app.py", "content": "print('hello')"},
        base_hash="sha256_base_12345",
        session_id="conv_abc123",
    )
    base_hash = compute_manifest_hash(base_manifest)

    # 1. Altered argument
    tampered_args = DeferredManifest(
        tool="sys_os_edit",
        arguments={"path": "app.py", "content": "print('malicious')"},
        base_hash="sha256_base_12345",
        session_id="conv_abc123",
    )
    assert compute_manifest_hash(tampered_args) != base_hash

    # 2. Altered base_hash
    tampered_base = DeferredManifest(
        tool="sys_os_edit",
        arguments={"path": "app.py", "content": "print('hello')"},
        base_hash="sha256_base_67890",
        session_id="conv_abc123",
    )
    assert compute_manifest_hash(tampered_base) != base_hash

    # 3. Altered target
    tampered_target = DeferredManifest(
        tool="sys_os_edit",
        arguments={"path": "app.py", "content": "print('hello')"},
        base_hash="sha256_base_12345",
        session_id="conv_abc123",
        target="other.py",
    )
    assert compute_manifest_hash(tampered_target) != base_hash
