"""Declare the runner-owned managed-artifact publishing tool."""

from __future__ import annotations

from omnigent_client import tool


@tool
def publish_design_artifact(
    entry_path: str,
    title: str,
    operation: str,
    summary: str = "",
) -> str:
    """Publish an HTML artifact through Omnigent's managed-artifact backend.

    Args:
        entry_path: Exact POSIX path to an HTML entry under ``artifacts/``.
        title: Concise user-facing artifact title.
        operation: Whether the artifact was ``created`` or ``updated``.
        summary: Optional one-sentence description of the artifact.
    """
    del entry_path, title, operation, summary
    raise RuntimeError("publish_design_artifact requires the managed artifact backend")
