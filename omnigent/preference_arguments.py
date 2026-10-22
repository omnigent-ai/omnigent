"""Resolve adopted native argument defaults once, where the session runs."""

from __future__ import annotations

from pathlib import Path

from omnigent.config import load_effective_config
from omnigent.harness_startup_config import resolve_harness_args
from omnigent.preference_startup import inherited_launch


def adopted_arguments(
    harness: str, workspace: str | Path, explicit: list[str] | None
) -> list[str] | None:
    """Keep legacy persisted arguments intact; prepend adopted defaults at launch."""
    if inherited_launch() is None:
        return explicit
    return resolve_harness_args(
        harness, tuple(explicit or ()), cfg=load_effective_config(workspace=workspace)
    )
