"""
Manifest hashing functions for tamper detection.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from omnigent.runtime.deferred.models import DeferredManifest


def _canonicalize(obj: Any) -> Any:
    """Recursively sort dict keys for deterministic JSON serialization."""
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_canonicalize(x) for x in obj]
    return obj


def compute_manifest_hash(manifest: DeferredManifest) -> str:
    """
    Compute deterministic SHA-256 hash of a deferred manifest.

    Canonicalizes dictionary keys to ensure identical payloads produce
    identical hash output across platforms.
    """
    canonical_payload = {
        "tool": manifest.tool,
        "arguments": _canonicalize(manifest.arguments),
        "base_hash": manifest.base_hash,
        "session_id": manifest.session_id,
        "target": manifest.target,
    }
    raw_bytes = json.dumps(
        canonical_payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()
