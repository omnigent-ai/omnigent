"""Publish metadata for a reviewed HTML design artifact."""

from __future__ import annotations

import json
import os
import stat
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

    artifact_dir = os.environ.get("OMNIGENT_ARTIFACT_DIR", "").strip()
    if not artifact_dir:
        raise RuntimeError("OMNIGENT_ARTIFACT_DIR is not configured")
    storage_root = Path(artifact_dir).expanduser()
    if not storage_root.is_dir():
        raise FileNotFoundError("managed artifact directory does not exist")

    entry = storage_root.joinpath(*normalized.parts[1:])
    current = storage_root
    for part in normalized.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"artifact entry does not exist: {entry_path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("artifact paths must not contain symlinks")
    if not entry.is_file():
        raise FileNotFoundError(f"artifact entry does not exist: {entry_path}")

    physical_root = entry.parent if entry.name == "index.html" else entry
    virtual_root = normalized.parent if normalized.name == "index.html" else normalized
    resource_count = (
        sum(1 for path in physical_root.rglob("*") if path.is_file() and not path.is_symlink())
        if physical_root.is_dir()
        else 1
    )
    payload = {
        "ok": True,
        "entry_path": normalized.as_posix(),
        "artifact_root": virtual_root.as_posix(),
        "title": title.strip(),
        "operation": operation,
        "language": "html",
        "resource_count": resource_count,
    }
    if summary.strip():
        payload["summary"] = summary.strip()
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
