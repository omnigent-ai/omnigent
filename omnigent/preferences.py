"""Workspace preference intent, independent of persistence and native settings.

Layers are immutable snapshots supplied by an authenticated caller. Resolving or
editing one never writes a config file, publishes a change, or checks whether a
harness is installed. Sync and native adapters own those separate operations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias


class PreferenceScope(StrEnum):
    BUILTIN = "builtin"
    MANAGED = "managed"
    SHARED = "shared"
    MACHINE = "machine"
    PROJECT = "project"
    SESSION = "session"


@dataclass(frozen=True)
class PreferenceIdentity:
    """Authenticated namespace; an endpoint URL alone is not an identity."""

    authority_id: str
    user_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        for value in (self.authority_id, self.user_id, self.workspace_id):
            _require_name(value)


@dataclass(frozen=True)
class PreferenceContext:
    identity: PreferenceIdentity
    installation_id: str

    def __post_init__(self) -> None:
        _require_name(self.installation_id)


@dataclass(frozen=True)
class PluginIdentity:
    """Native adapter identity; two sources may publish the same plugin name."""

    ecosystem: str
    source: str
    name: str

    def __post_init__(self) -> None:
        for value in (self.ecosystem, self.source, self.name):
            _require_name(value)


class PreferenceKind(StrEnum):
    MODEL = "model"
    HARNESS = "harness"
    HARNESS_ARGS = "harness_args"
    PLUGIN = "plugin"


@dataclass(frozen=True)
class PreferenceKey:
    """One merge unit: a model, default harness, ordered args, or plugin toggle."""

    kind: PreferenceKind
    name: str | PluginIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PreferenceKind):
            raise ValueError("preference kind must be a PreferenceKind")
        if self.kind == PreferenceKind.PLUGIN:
            if not isinstance(self.name, PluginIdentity):
                raise ValueError("plugin preferences require a PluginIdentity")
            return
        _require_name(self.name)
        if self.kind == PreferenceKind.MODEL and self.name not in ("model", "opencode_model"):
            raise ValueError("unknown model preference key")
        if self.kind == PreferenceKind.HARNESS and self.name != "default":
            raise ValueError("the harness preference key must be 'default'")
        if self.kind == PreferenceKind.HARNESS_ARGS:
            from omnigent.harness_aliases import canonicalize_harness

            assert isinstance(self.name, str)
            canonical = canonicalize_harness(self.name) or self.name
            if canonical == "default":
                raise ValueError("'default' is reserved for the default harness selection")
            object.__setattr__(self, "name", canonical)


PreferenceValue: TypeAlias = str | bool | tuple[str, ...]


def _require_name(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("preference identifiers must be non-empty strings")


def _validate_value(key: PreferenceKey, value: PreferenceValue) -> None:
    if key.kind == PreferenceKind.PLUGIN:
        valid = isinstance(value, bool)
    elif key.kind == PreferenceKind.HARNESS_ARGS:
        valid = isinstance(value, tuple) and all(isinstance(arg, str) for arg in value)
    else:
        valid = isinstance(value, str) and bool(value.strip())
    if not valid:
        raise ValueError(f"invalid value for {key.kind.value} preference")


@dataclass(frozen=True)
class PreferenceLayer:
    """An immutable snapshot, with machine overrides tied to one installation."""

    scope: PreferenceScope
    identity: PreferenceIdentity
    values: Mapping[PreferenceKey, PreferenceValue] = field(default_factory=dict)
    installation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, PreferenceScope):
            raise ValueError("preference scope must be a PreferenceScope")
        if self.scope == PreferenceScope.MACHINE:
            _require_name(self.installation_id)
        elif self.installation_id is not None:
            raise ValueError("only machine preferences have an installation ID")
        values = dict(self.values)
        for key, value in values.items():
            _validate_value(key, value)
        object.__setattr__(self, "values", MappingProxyType(values))

    def with_value(self, key: PreferenceKey, value: PreferenceValue) -> PreferenceLayer:
        """Return an explicit edit to this scope; persistence is the caller's job."""
        return replace(self, values={**self.values, key: value})

    def without(self, key: PreferenceKey) -> PreferenceLayer:
        """Remove an override so resolution can inherit a lower layer again."""
        return replace(self, values={k: v for k, v in self.values.items() if k != key})


@dataclass(frozen=True)
class ResolvedPreference:
    """Requested value and source, before availability or policy checks."""

    value: PreferenceValue
    source: PreferenceScope


def resolve_preferences(
    context: PreferenceContext, layers: Sequence[PreferenceLayer]
) -> dict[PreferenceKey, ResolvedPreference]:
    """Resolve per setting, rejecting foreign identities and ambiguous scopes.

    Session > project > machine > shared > managed defaults > built-ins.
    An empty argument tuple and a disabled plugin are explicit values. Missing
    entries inherit; availability must not silently replace the requested value.
    """
    by_scope: dict[PreferenceScope, PreferenceLayer] = {}
    for layer in layers:
        if layer.identity != context.identity:
            raise ValueError("preference layer belongs to a different identity")
        if layer.scope == PreferenceScope.MACHINE and (
            layer.installation_id != context.installation_id
        ):
            raise ValueError("machine preferences belong to a different installation")
        if layer.scope in by_scope:
            raise ValueError(f"duplicate preference scope: {layer.scope.value}")
        by_scope[layer.scope] = layer
    resolved: dict[PreferenceKey, ResolvedPreference] = {}
    for scope in PreferenceScope:
        if layer := by_scope.get(scope):
            for key, value in layer.values.items():
                resolved[key] = ResolvedPreference(value, scope)
    return resolved


def preferences_from_config(cfg: Mapping[str, object]) -> dict[PreferenceKey, PreferenceValue]:
    """Extract portable legacy values for a preview or project resolution.

    Extraction is read-only and never adopts legacy values as machine overrides.
    Native plugin settings need an ecosystem adapter and are not in this YAML.
    """
    from omnigent.harness_startup_config import resolve_harness_config

    values: dict[PreferenceKey, PreferenceValue] = {}
    for name in ("model", "opencode_model"):
        value = cfg.get(name)
        if isinstance(value, str) and value.strip():
            values[PreferenceKey(PreferenceKind.MODEL, name)] = value
    default, overrides = resolve_harness_config(cfg)
    if default and default.strip():
        values[PreferenceKey(PreferenceKind.HARNESS, "default")] = default
    for harness, entry in overrides.items():
        if "args" in entry:
            values[PreferenceKey(PreferenceKind.HARNESS_ARGS, harness)] = tuple(entry["args"])
    return values
