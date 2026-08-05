from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from issue_prioritization.domain import Issue


@dataclass(frozen=True)
class Area:
    key: str
    label: str
    weight: Decimal
    definition: str = ""
    priority_label: str | None = None

    @property
    def issue_label(self) -> str:
        return self.priority_label or self.label


@dataclass(frozen=True)
class AreaCatalog:
    by_key: Mapping[str, Area]
    by_label: Mapping[str, tuple[Area, ...]]

    @classmethod
    def from_json(cls, path: str | Path) -> AreaCatalog:
        value = json.loads(Path(path).read_text())
        raw_areas = value.get("areas")
        if not isinstance(raw_areas, list):
            raise ValueError("areas.json must contain an areas array")

        areas = []
        for raw_area in raw_areas:
            if not isinstance(raw_area, Mapping):
                raise ValueError("each area must be an object")
            areas.append(
                Area(
                    key=str(raw_area["key"]),
                    label=str(raw_area["label"]),
                    weight=Decimal(str(raw_area["weight"])),
                    definition=str(raw_area.get("definition", "")),
                    priority_label=str(raw_area.get("priority_label") or raw_area["label"]),
                )
            )

        by_label: dict[str, list[Area]] = {}
        for area in areas:
            by_label.setdefault(area.label, []).append(area)
            if area.issue_label != area.label:
                by_label.setdefault(area.issue_label, []).append(area)
        return cls(
            by_key={area.key: area for area in areas},
            by_label={label: tuple(items) for label, items in by_label.items()},
        )

    def weight_for(self, issue: Issue, default: Decimal) -> Decimal:
        exact = [self.by_key[key].weight for key in issue.area_keys if key in self.by_key]
        if exact:
            return max(exact)

        fallback = [
            area.weight for label in issue.component_labels for area in self.by_label.get(label, ())
        ]
        return max(fallback, default=default)
