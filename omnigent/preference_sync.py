"""Versioned shared-preference protocol, independent of transport and storage."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from omnigent.preferences import (
    PluginIdentity,
    PreferenceIdentity,
    PreferenceKey,
    PreferenceKind,
    PreferenceLayer,
    PreferenceScope,
)

Name = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")]
Version = Annotated[StrictInt, Field(ge=0, le=2**53 - 1)]
Value = StrictStr | StrictBool | list[StrictStr] | None
PREFERENCE_PAGE_SIZE = 100
MAX_KEYS = 512
MAX_OPERATIONS = 10000
MAX_CONFLICTS = 128
MAX_VALUE_BYTES = 8192
MAX_RECORD_BYTES = 16000


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreferenceNamespace(ProtocolModel):
    authority_id: Name
    user_id: Name
    workspace_id: Name

    def as_identity(self) -> PreferenceIdentity:
        return PreferenceIdentity(self.authority_id, self.user_id, self.workspace_id)


class PluginName(ProtocolModel):
    ecosystem: Name
    source: Name
    name: Name


class SharedPreferenceKey(ProtocolModel):
    kind: PreferenceKind
    name: Name | PluginName

    def as_key(self) -> PreferenceKey:
        name = (
            PluginIdentity(**self.name.model_dump())
            if isinstance(self.name, PluginName)
            else self.name
        )
        return PreferenceKey(self.kind, name)

    @model_validator(mode="after")
    def canonicalize(self) -> Self:
        key = self.as_key()
        if isinstance(key.name, str):
            object.__setattr__(self, "name", key.name)
        return self

    def storage_key(self) -> str:
        name = self.name
        encoded_name = (
            {"ecosystem": name.ecosystem, "source": name.source, "name": name.name}
            if isinstance(name, PluginName)
            else name
        )
        encoded = json.dumps(
            {"kind": self.kind.value, "name": encoded_name},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


class PreferenceMutation(ProtocolModel):
    protocol_version: Literal[1]
    identity: PreferenceNamespace
    operation_id: UUID
    installation_id: Name
    key: SharedPreferenceKey
    expected_version: Version
    value: Value  # null removes a preference; false and [] remain explicit values.
    resolves: list[UUID] = Field(default_factory=list, max_length=MAX_CONFLICTS)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if len(self.model_dump_json(include={"value"}).encode()) > MAX_VALUE_BYTES:
            raise ValueError("encoded preference value envelope exceeds 8192 bytes")
        if self.value is not None:
            value = tuple(self.value) if isinstance(self.value, list) else self.value
            PreferenceLayer(
                PreferenceScope.SHARED, self.identity.as_identity(), {self.key.as_key(): value}
            )
        if len(self.resolves) != len(set(self.resolves)):
            raise ValueError("conflict IDs must be unique")
        return self


class SharedPreferenceEntry(ProtocolModel):
    key: SharedPreferenceKey
    version: Version
    value: Value
    installation_id: Name | None = None


class PreferenceResult(ProtocolModel):
    protocol_version: Literal[1] = 1
    operation_id: UUID
    status: Literal["applied", "unchanged", "conflict"]
    revision: Version
    entry: SharedPreferenceEntry


class PreferenceSnapshot(ProtocolModel):
    protocol_version: Literal[1] = 1
    identity: PreferenceNamespace
    revision: Version = 0
    entries: list[SharedPreferenceEntry] = Field(default_factory=list)
    conflicts: list[PreferenceMutation] = Field(default_factory=list)

    def as_layer(self) -> PreferenceLayer:
        """Reserved for explicit client adoption of a complete acknowledged snapshot."""
        return PreferenceLayer(
            PreferenceScope.SHARED,
            self.identity.as_identity(),
            {
                entry.key.as_key(): tuple(entry.value)
                if isinstance(entry.value, list)
                else entry.value
                for entry in self.entries
                if entry.value is not None
            },
        )


class PreferencePage(ProtocolModel):
    """One revision-bound page; only a complete traversal forms a snapshot."""

    protocol_version: Literal[1] = 1
    identity: PreferenceNamespace
    revision: Version = 0
    entries: list[SharedPreferenceEntry] = Field(
        default_factory=list, max_length=PREFERENCE_PAGE_SIZE
    )
    conflicts: list[PreferenceMutation] = Field(
        default_factory=list, max_length=PREFERENCE_PAGE_SIZE
    )
    next_cursor: str | None
