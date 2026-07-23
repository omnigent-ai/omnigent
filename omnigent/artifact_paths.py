"""Shared contracts for user-visible design artifact entry paths."""

from __future__ import annotations


def canonical_artifact_entry_path(entry_path: object) -> tuple[str, str]:
    """Validate and return ``(entry_path, artifact_root)`` for a previewable artifact."""
    if not isinstance(entry_path, str) or not entry_path or "\\" in entry_path:
        raise ValueError("entry_path must be a relative POSIX path")
    if entry_path.startswith("/"):
        raise ValueError("entry_path must be a relative POSIX path")
    parts = entry_path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("entry_path must be normalized")
    if parts[0] != "artifacts":
        raise ValueError("entry_path must point to HTML under artifacts/")
    if len(parts) == 2 and parts[1].lower().endswith(".html"):
        return entry_path, entry_path
    if len(parts) == 3 and parts[2] == "index.html":
        return entry_path, "/".join(parts[:2])
    raise ValueError("entry_path must be artifacts/<slug>.html or artifacts/<slug>/index.html")
