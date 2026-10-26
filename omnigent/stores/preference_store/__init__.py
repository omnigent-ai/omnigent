"""Authenticated workspace preference persistence contract."""

from typing import Protocol

from omnigent.configuration_profile import ConfigurationSave, ConfigurationSnapshot
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

    def read_configuration(self, *, user_id: str | None) -> ConfigurationSnapshot: ...

    def save_configuration(
        self, operation: ConfigurationSave, *, user_id: str | None
    ) -> ConfigurationSnapshot: ...
