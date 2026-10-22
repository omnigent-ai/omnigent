"""Portable configuration snapshots for explicit save and import."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Self

from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from omnigent.preference_sync import Name, PreferenceNamespace, ProtocolModel, Version
from omnigent.preferences import PreferenceKey, PreferenceKind

MAX_CONFIGURATION_BYTES = 12000


class PluginOptOuts(ProtocolModel):
    """Existing launcher preferences, not native plugin installation state."""

    opted_out: list[Name] = Field(max_length=128)
    disable_all: StrictBool

    @field_validator("opted_out")
    @classmethod
    def normalize_names(cls, names: list[str]) -> list[str]:
        if any("@" in name or name != name.strip() for name in names):
            raise ValueError("Plugin opt-outs require bare plugin names")
        return sorted(set(names))


class PortableConfiguration(ProtocolModel):
    model: Name | None = None
    opencode_model: Name | None = None
    default_harness: Name | None = None
    harness_args: dict[Name, list[StrictStr]] = Field(default_factory=dict, max_length=64)
    plugin_opt_outs: PluginOptOuts | None = None

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        canonical = {}
        for name, arguments in self.harness_args.items():
            key = PreferenceKey(PreferenceKind.HARNESS_ARGS, name)
            if key.name in canonical:
                raise ValueError("Duplicate harness aliases in configuration")
            canonical[key.name] = arguments
        object.__setattr__(self, "harness_args", canonical)
        if len(self.model_dump_json().encode()) > MAX_CONFIGURATION_BYTES:
            raise ValueError("Portable configuration exceeds 12000 bytes")
        return self

    @classmethod
    def from_config(
        cls, config: Mapping[str, object], *, plugin_opt_outs: PluginOptOuts | None = None
    ) -> PortableConfiguration:
        if not isinstance(config, Mapping):
            raise ValueError("Local configuration must be a mapping")
        harness = config.get("harness")
        arguments = {}
        if isinstance(harness, Mapping):
            default = harness.get("default")
            for name, settings in harness.items():
                if name == "default" or settings is None:
                    continue
                if not isinstance(settings, Mapping):
                    raise ValueError(f"Local harness configuration is not a mapping: {name}")
                if settings.get("args") is not None:
                    key = PreferenceKey(PreferenceKind.HARNESS_ARGS, name)
                    arguments[key.name] = settings["args"]
        elif harness is None or isinstance(harness, str):
            default = harness
        else:
            raise ValueError("Local harness configuration must be a string or mapping")
        return cls.model_validate(
            {
                "model": config.get("model"),
                "opencode_model": config.get("opencode_model"),
                "default_harness": default,
                "harness_args": arguments,
                "plugin_opt_outs": plugin_opt_outs,
            }
        )

    def apply_to(self, config: Mapping[str, object]) -> dict[str, object]:
        """Replace only portable defaults, retaining local commands and credentials."""
        result = deepcopy(dict(config))
        for key in ("model", "opencode_model"):
            value = getattr(self, key)
            if value is None:
                result.pop(key, None)
            else:
                result[key] = value
        previous = result.get("harness")
        harness = deepcopy(previous) if isinstance(previous, dict) else {}
        harness.pop("default", None)
        for name, settings in list(harness.items()):
            if isinstance(settings, dict):
                settings.pop("args", None)
                if not settings:
                    del harness[name]
        if self.default_harness is not None:
            harness["default"] = self.default_harness
        for name, arguments in self.harness_args.items():
            settings = harness.setdefault(name, {})
            if not isinstance(settings, dict):
                raise ValueError(f"Local harness configuration is not a mapping: {name}")
            settings["args"] = list(arguments)
        if harness:
            result["harness"] = harness
        else:
            result.pop("harness", None)
        return result


class ConfigurationSnapshot(ProtocolModel):
    protocol_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    identity: PreferenceNamespace
    revision: Version = 0
    configuration: PortableConfiguration | None = None


class ConfigurationSave(ProtocolModel):
    identity: PreferenceNamespace
    expected_revision: Version
    configuration: PortableConfiguration
