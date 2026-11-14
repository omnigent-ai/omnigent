"""Charts an agent can attach to an Inbox item.

An agent cannot post UI code, so an item may carry a small declarative chart
that the web app knows how to draw. The server validates it here, and the
validation errors are returned to the agent so it can fix and retry. New
chart types or fields add a new ``version``; the server accepts every version
the web app can draw, and the web app skips versions it does not know.
"""

from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CHART_X_LABELS = 60
MAX_CHART_SERIES = 4
MAX_CHART_MARKERS = 3
# Serialized size limit, below the column's 16,383 stored-byte cap.
MAX_CHART_JSON_BYTES = 16_000


class ChartSeries(BaseModel):
    """One line or set of bars; ``None`` values leave gaps."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    values: list[float | None]
    emphasis: bool = False

    @field_validator("values")
    @classmethod
    def _finite(cls, values: list[float | None]) -> list[float | None]:
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("series values must be finite numbers or null")
        return values


class ChartHighlight(BaseModel):
    """A shaded range between two x labels."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start: str = Field(alias="from", max_length=40)
    end: str = Field(alias="to", max_length=40)


class ChartMarker(BaseModel):
    """A labeled vertical line at one x label, e.g. when a change merged."""

    model_config = ConfigDict(extra="forbid")

    x: str = Field(max_length=40)
    label: str = Field(min_length=1, max_length=60)


class ChartSource(BaseModel):
    """Where the numbers came from, shown under the chart."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=100)
    href: str | None = Field(default=None, max_length=2048, pattern=r"^https?://")


class InboxChartV1(BaseModel):
    """Version 1 of the chart an Inbox item can carry."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: Literal[1]
    type: Literal["line", "bar"]
    title: str = Field(min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=16)
    x: list[str] = Field(min_length=1, max_length=MAX_CHART_X_LABELS)
    series: list[ChartSeries] = Field(min_length=1, max_length=MAX_CHART_SERIES)
    highlight: ChartHighlight | None = None
    markers: list[ChartMarker] = Field(default_factory=list, max_length=MAX_CHART_MARKERS)
    source: ChartSource | None = None

    @field_validator("x")
    @classmethod
    def _short_labels(cls, labels: list[str]) -> list[str]:
        if any(len(label) > 40 for label in labels):
            raise ValueError("x labels must be at most 40 characters")
        return labels

    @model_validator(mode="after")
    def _consistent(self) -> InboxChartV1:
        labels = set(self.x)
        for series in self.series:
            if len(series.values) != len(self.x):
                raise ValueError(f"series {series.name!r} needs one value per x label")
        if self.highlight is not None and not {self.highlight.start, self.highlight.end} <= labels:
            raise ValueError("highlight from and to must be x labels")
        for marker in self.markers:
            if marker.x not in labels:
                raise ValueError(f"marker x {marker.x!r} is not an x label")
        if len(json.dumps(self.stored()).encode("utf-8")) > MAX_CHART_JSON_BYTES:
            raise ValueError(f"chart is larger than {MAX_CHART_JSON_BYTES} bytes of JSON")
        return self

    def stored(self) -> dict[str, object]:
        """The chart as stored and returned, using the wire field names."""
        return self.model_dump(by_alias=True, exclude_none=True)
