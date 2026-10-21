"""Authenticated workspace preference persistence contract."""

from typing import Literal, Protocol

from omnigent.preference_sync import (
    MAX_CONFLICTS,
    MAX_KEYS,
    PREFERENCE_PAGE_SIZE,
    PreferenceMutation,
    PreferenceNamespace,
    PreferencePage,
    PreferenceResult,
    PreferenceSnapshot,
)


class PreferenceStore(Protocol):
    def identity(self, user_id: str | None) -> PreferenceNamespace: ...

    def read(
        self,
        *,
        user_id: str | None,
        limit: int = PREFERENCE_PAGE_SIZE,
        cursor: str | None = None,
        protocol_version: Literal[1, 2] = 1,
    ) -> PreferencePage: ...

    def apply(self, operation: PreferenceMutation, *, user_id: str | None) -> PreferenceResult: ...


def read_store_snapshot(store: PreferenceStore, user_id: str | None) -> PreferenceSnapshot:
    """Collect the bounded, revision-consistent view for a session preparation."""
    snapshot = PreferenceSnapshot(identity=store.identity(user_id))
    cursor = None
    for index in range((MAX_KEYS + MAX_CONFLICTS) // PREFERENCE_PAGE_SIZE + 1):
        page = store.read(user_id=user_id, cursor=cursor, protocol_version=2)
        if index == 0:
            snapshot = snapshot.model_copy(
                update={"revision": page.revision, "protocol_version": page.protocol_version}
            )
        if page.identity != snapshot.identity or page.revision != snapshot.revision:
            raise ValueError("Shared preferences changed while loading; retry")
        snapshot.entries.extend(page.entries)
        snapshot.conflicts.extend(page.conflicts)
        cursor = page.next_cursor
        if cursor is None:
            return snapshot
    raise ValueError("Preference snapshot exceeds its limits")
