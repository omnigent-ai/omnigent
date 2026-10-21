"""Validation and transport of the portable defaults saved with one session."""

from __future__ import annotations

from typing import Any, TypedDict

from omnigent.db.compression import encode
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.preference_startup import PreferenceLaunch


def encode_preference_snapshot(snapshot: dict[str, Any] | None) -> str | None:
    """Validate before either conversation database is written; fit every supported DB."""
    if snapshot is None:
        return None
    launch = PreferenceLaunch.model_validate(snapshot)
    if launch.shared.revision == 0:
        raise ValueError("Session preferences require an adopted workspace")
    encoded = launch.model_dump_json()
    stored = encode(encoded)
    if stored is not None and len(stored) > 65_535:
        raise OmnigentError(
            "Preference snapshot exceeds the 65,535-byte compressed storage limit.",
            code=ErrorCode.INVALID_INPUT,
        )
    return encoded


def session_preference_snapshot() -> dict[str, Any] | None:
    """Carry the CLI's already-reviewed choice without refreshing during upload."""
    import click

    from omnigent.preference_startup import inherited_launch

    context = click.get_current_context(silent=True)
    if context is not None:
        if context.meta.get("temporary_preferences"):
            return None
        launches = [
            value
            for key, value in context.meta.items()
            if key.startswith("preferences:") and isinstance(value, PreferenceLaunch)
        ]
        if len(launches) > 1:
            raise ValueError("CLI preference snapshot has ambiguous server identity")
        if launches:
            return launches[0].model_dump(mode="json")
    launch = inherited_launch()
    return launch.model_dump(mode="json") if launch is not None else None


class SessionPreferenceMetadata(TypedDict, total=False):
    preference_snapshot: dict[str, Any]


def session_preference_metadata() -> SessionPreferenceMetadata:
    snapshot = session_preference_snapshot()
    return {"preference_snapshot": snapshot} if snapshot is not None else {}
