"""Typed owner controls for a host's portable preferences; no arbitrary file access."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from omnigent.preference_client import MAX_PENDING_OPERATIONS
from omnigent.preference_startup import PreferenceLaunch
from omnigent.preference_sync import (
    MAX_KEYS,
    PreferenceMutation,
    PreferenceSnapshot,
    ProtocolModel,
    SharedPreferenceEntry,
    SharedPreferenceKey,
    Value,
)
from omnigent.preference_workspace import ImportPreview


class PreferenceInspect(ProtocolModel):
    action: Literal["status", "connect", "sync", "preview_restore"]


class PreferenceImport(ProtocolModel):
    action: Literal["preview_import"]
    plugins: list[Literal["claude", "codex"]] = Field(default_factory=list, max_length=2)


class PreferenceApply(ProtocolModel):
    action: Literal["apply_import"]
    preview: ImportPreview


class PreferenceMachine(ProtocolModel):
    action: Literal["set_machine"]
    key: SharedPreferenceKey
    value: Value


class PreferenceDiscard(ProtocolModel):
    action: Literal["discard"]
    operation_id: UUID


class PreferencePrepare(ProtocolModel):
    action: Literal["prepare"]
    workspace: str | None = Field(default=None, max_length=4096)


class PreferenceDefaults(ProtocolModel):
    model: str | None = None
    opencode_model: str | None = None
    default_harness: str | None = None


PreferenceControl = Annotated[
    PreferenceInspect
    | PreferenceImport
    | PreferenceApply
    | PreferenceMachine
    | PreferenceDiscard
    | PreferencePrepare,
    Field(discriminator="action"),
]
PREFERENCE_CONTROL = TypeAdapter(PreferenceControl)


class PreferenceControlResult(ProtocolModel):
    connected: bool
    shared: PreferenceSnapshot | None = None
    machine: list[SharedPreferenceEntry] = Field(default_factory=list, max_length=MAX_KEYS)
    pending: list[PreferenceMutation] = Field(
        default_factory=list, max_length=MAX_PENDING_OPERATIONS
    )
    preview: ImportPreview | None = None
    warning: str | None = Field(default=None, max_length=1024)
    launch: PreferenceLaunch | None = None
    defaults: PreferenceDefaults | None = None
