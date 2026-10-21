"""Authenticated workspace preference persistence contract."""

from typing import Protocol

from omnigent.preference_sync import (
    PREFERENCE_PAGE_SIZE,
    PreferenceMutation,
    PreferenceNamespace,
    PreferencePage,
    PreferenceResult,
)


class PreferenceStore(Protocol):
    def identity(self, user_id: str | None) -> PreferenceNamespace: ...

    def read(
        self, *, user_id: str | None, limit: int = PREFERENCE_PAGE_SIZE, cursor: str | None = None
    ) -> PreferencePage: ...

    def apply(self, operation: PreferenceMutation, *, user_id: str | None) -> PreferenceResult: ...
