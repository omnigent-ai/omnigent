from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from issue_prioritization.domain import Impact, Priority


@dataclass(frozen=True)
class ModuleConfig:
    enabled: bool
    values: Mapping[str, Decimal]

    def decimal(self, name: str) -> Decimal:
        return self.values[name]


@dataclass(frozen=True)
class ScoringConfig:
    impact_weights: Mapping[Impact, Decimal]
    priority_thresholds: Mapping[Priority, Decimal]
    module_order: tuple[str, ...]
    modules: Mapping[str, ModuleConfig]

    @classmethod
    def default(cls) -> ScoringConfig:
        resource = files("issue_prioritization").joinpath("default_scoring.json")
        return cls.from_mapping(json.loads(resource.read_text()))

    @classmethod
    def from_json(cls, path: str | Path) -> ScoringConfig:
        return cls.from_mapping(json.loads(Path(path).read_text()))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ScoringConfig:
        impact_values = _mapping_alias(value, "impact_weights", "severity_weights")
        threshold_values = _mapping(value, "priority_thresholds")
        module_values = _mapping(value, "modules")
        modules: dict[str, ModuleConfig] = {}
        for name, raw_module in module_values.items():
            if not isinstance(raw_module, Mapping):
                raise ValueError(f"module {name!r} must be an object")
            enabled = bool(raw_module.get("enabled", False))
            values = {
                str(key): _decimal(raw_value)
                for key, raw_value in raw_module.items()
                if key != "enabled"
            }
            modules[str(name)] = ModuleConfig(enabled=enabled, values=values)

        raw_order = value.get("module_order", ())
        if not isinstance(raw_order, list):
            raise ValueError("module_order must be an array")

        config = cls(
            impact_weights={
                Impact.parse(name): _decimal(weight) for name, weight in impact_values.items()
            },
            priority_thresholds={
                Priority(str(name)): _decimal(threshold)
                for name, threshold in threshold_values.items()
            },
            module_order=tuple(str(name) for name in raw_order),
            modules=modules,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if set(self.impact_weights) != set(Impact):
            raise ValueError("impact_weights must define critical, high, medium, and low")
        if set(self.priority_thresholds) != set(Priority):
            raise ValueError("priority_thresholds must define P0-P3")
        missing = set(self.module_order) - set(self.modules)
        if missing:
            raise ValueError(f"module_order references missing modules: {sorted(missing)}")

    def priority_for(self, score: Decimal) -> Priority:
        for priority in (Priority.P0, Priority.P1, Priority.P2, Priority.P3):
            if score >= self.priority_thresholds[priority]:
                return priority
        return Priority.P3

    def as_dict(self) -> dict[str, object]:
        return {
            "impact_weights": {
                impact.value: _json_number(weight) for impact, weight in self.impact_weights.items()
            },
            "priority_thresholds": {
                priority.value: _json_number(threshold)
                for priority, threshold in self.priority_thresholds.items()
            },
            "module_order": list(self.module_order),
            "modules": {
                name: {
                    "enabled": module.enabled,
                    **{key: _json_number(value) for key, value in module.values.items()},
                }
                for name, module in self.modules.items()
            },
        }


def _mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise ValueError(f"{name} must be an object")
    return result


def _mapping_alias(
    value: Mapping[str, object],
    name: str,
    legacy_name: str,
) -> Mapping[str, object]:
    result = value.get(name, value.get(legacy_name))
    if not isinstance(result, Mapping):
        raise ValueError(f"{name} must be an object")
    return result


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected number, got {value!r}")
    return Decimal(str(value))


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)
