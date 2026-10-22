"""Write a caller-owned frozen preference handoff for production startup tests."""

from pathlib import Path

from omnigent.preference_startup import PreferenceLaunch, write_launch_snapshot
from omnigent.preference_sync import PreferenceNamespace, PreferenceSnapshot, SharedPreferenceEntry


def preference_snapshot(
    root: Path,
    *,
    identity: PreferenceNamespace,
    shared: list[SharedPreferenceEntry],
    machine: list[SharedPreferenceEntry] | None = None,
    installation_id: str = "test",
) -> Path:
    path = root / "preferences.json"
    write_launch_snapshot(
        path,
        PreferenceLaunch(
            shared=PreferenceSnapshot(identity=identity, revision=1, entries=shared),
            machine=machine or [],
            installation_id=installation_id,
            state="current",
        ),
    )
    return path
