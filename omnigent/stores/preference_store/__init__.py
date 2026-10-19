"""Authenticated workspace preference persistence contract."""

from typing import Protocol

from omnigent.preference_sync import (
    PreferenceMutation,
    PreferenceNamespace,
    PreferenceResult,
    PreferenceSnapshot,
)


class PreferenceStore(Protocol):
    def identity(self, user_id: str | None) -> PreferenceNamespace: ...

    def read(self, *, user_id: str | None) -> PreferenceSnapshot: ...

    def apply(self, operation: PreferenceMutation, *, user_id: str | None) -> PreferenceResult: ...
