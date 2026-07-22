"""Publish metadata for a reviewed HTML design artifact."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from omnigent_client import tool


@tool
def publish_design_artifact(
    entry_path: str,
    title: str,
    operation: str,
    summary: str = "",
) -> str:
    """Publish a reviewed HTML artifact entry point for the Omnigent UI.

    Args:
        entry_path: Exact POSIX path to an HTML entry under ``artifacts/``.
        title: Concise user-facing artifact title.
        operation: Whether the artifact was ``created`` or ``updated``.
        summary: Optional one-sentence description of the artifact.
    """
    normalized = PurePosixPath(entry_path)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("entry_path must be a relative path under artifacts/")
    if not normalized.parts or normalized.parts[0] != "artifacts":
        raise ValueError("entry_path must be under artifacts/")
    if normalized.suffix.lower() != ".html":
        raise ValueError("entry_path must point to an HTML file")
    if operation not in {"created", "updated"}:
        raise ValueError("operation must be 'created' or 'updated'")
    if not title.strip():
        raise ValueError("title must not be empty")

    entry = Path(*normalized.parts)
    if not entry.is_file():
        raise FileNotFoundError(f"artifact entry does not exist: {entry_path}")

    artifact_root = entry.parent if entry.name == "index.html" else entry
    resource_count = (
        sum(1 for path in artifact_root.rglob("*") if path.is_file())
        if artifact_root.is_dir()
        else 1
    )
    payload = {
        "ok": True,
        "entry_path": normalized.as_posix(),
        "artifact_root": PurePosixPath(artifact_root.as_posix()).as_posix(),
        "title": title.strip(),
        "operation": operation,
        "language": "html",
        "resource_count": resource_count,
    }
    if summary.strip():
        payload["summary"] = summary.strip()
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
