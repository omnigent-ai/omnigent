from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Remove this cleanup list in v0.3.0 after the apply backfill completes.
LEGACY_SEVERITY_LABELS = frozenset(f"severity:S{level}" for level in range(4))


@dataclass(frozen=True)
class LabelDefinition:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class LabelManifest:
    labels: tuple[LabelDefinition, ...]

    @classmethod
    def from_json(cls, path: str | Path) -> LabelManifest:
        value = json.loads(Path(path).read_text())
        return cls(
            labels=tuple(
                LabelDefinition(
                    name=str(item["name"]),
                    color=str(item["color"]),
                    description=str(item["description"]),
                )
                for item in value["labels"]
            )
        )

    @property
    def component_labels(self) -> set[str]:
        return {label.name for label in self.labels if label.name.startswith("comp:")}
