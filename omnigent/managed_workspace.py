"""Logical workspace identifiers for host-managed repositories."""

from __future__ import annotations

_MANAGED_WORKSPACE_PREFIX = "managed://"


def managed_workspace(repo_id: str) -> str:
    """Return the durable workspace marker for a managed repository."""
    canonical_repo_id = repo_id.strip()
    if not canonical_repo_id:
        raise ValueError("managed repository id must not be empty")
    return f"{_MANAGED_WORKSPACE_PREFIX}{canonical_repo_id}"


def parse_managed_workspace(workspace: str) -> str | None:
    """Return the managed repository id, or ``None`` for a normal path."""
    if not workspace.startswith(_MANAGED_WORKSPACE_PREFIX):
        return None
    repo_id = workspace.removeprefix(_MANAGED_WORKSPACE_PREFIX)
    if not repo_id:
        raise ValueError("managed workspace marker has no repository id")
    return repo_id
