"""Bounded, identity-free Codex subscription rate-limit snapshots."""

from __future__ import annotations

import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnigent.util.json_types import JsonObject

RATE_LIMITS_TTL_S = 3600
_MAX_BUCKETS, _MAX_TEXT, _MAX_MINUTES = 16, 128, 5 * 525_600
_MAX_JSON_INT = (1 << 53) - 1
_PositiveInt = Annotated[int, Field(gt=0, le=_MAX_JSON_INT)]


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Window(_WireModel):
    kind: Literal["primary", "secondary"]
    used_percent: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
    window_duration_mins: Annotated[int, Field(gt=0, le=_MAX_MINUTES)]
    resets_at: _PositiveInt | None = None


class _Bucket(_WireModel):
    limit_id: Annotated[str, Field(min_length=1, max_length=_MAX_TEXT)]
    limit_name: Annotated[str, Field(min_length=1, max_length=_MAX_TEXT)] | None = None
    windows: Annotated[list[_Window], Field(min_length=1, max_length=2)]

    @field_validator("limit_id", "limit_name")
    @classmethod
    def _no_identity(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or "@" in value):
            raise ValueError("identity-like rate-limit label")
        return value


class _Snapshot(_WireModel):
    captured_at: _PositiveInt
    limits: Annotated[list[_Bucket], Field(min_length=1, max_length=_MAX_BUCKETS)]


def validate_rate_limits(snapshot: object) -> JsonObject | None:
    if snapshot is None:
        return None
    try:
        return _Snapshot.model_validate(snapshot).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ValueError("invalid Codex rate-limit snapshot") from exc


def _window(raw: object, kind: str) -> JsonObject | None:
    if not isinstance(raw, dict):
        return None
    window: JsonObject = {
        "kind": kind,
        "used_percent": raw.get("usedPercent"),
        "window_duration_mins": raw.get("windowDurationMins"),
    }
    reset = raw.get("resetsAt")
    if isinstance(reset, int) and not isinstance(reset, bool) and 0 < reset <= _MAX_JSON_INT:
        window["resets_at"] = reset
    try:
        return _Window.model_validate(window).model_dump(exclude_none=True)
    except ValidationError:
        return None


def normalize_rate_limits(
    response: object, *, captured_at: int | None = None
) -> JsonObject | None:
    """Discard account identity, credits, auth fields, and malformed windows."""
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return None
    rows: list[tuple[str, dict[object, object]]] = []
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for index, (raw_id, bucket) in enumerate(by_id.items()):
            if index >= _MAX_BUCKETS * 4:
                break
            limit_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if (
                limit_id
                and "@" not in limit_id
                and len(limit_id) <= _MAX_TEXT
                and isinstance(bucket, dict)
            ):
                rows.append((limit_id, bucket))
                if len(rows) == _MAX_BUCKETS:
                    break
    if not rows and isinstance(result.get("rateLimits"), dict):
        bucket = result["rateLimits"]
        raw_id = bucket.get("limitId")
        limit_id = raw_id.strip() if isinstance(raw_id, str) else "codex"
        if limit_id and "@" not in limit_id and len(limit_id) <= _MAX_TEXT:
            rows.append((limit_id, bucket))
    limits: list[JsonObject] = []
    for limit_id, bucket in rows:
        windows = [
            window
            for kind in ("primary", "secondary")
            if (window := _window(bucket.get(kind), kind)) is not None
        ]
        if windows:
            item: JsonObject = {"limit_id": limit_id, "windows": windows}
            name = bucket.get("limitName")
            if isinstance(name, str) and name.strip() and "@" not in name:
                item["limit_name"] = name.strip()[:_MAX_TEXT]
            limits.append(item)
    if not limits:
        return None
    try:
        return validate_rate_limits(
            {
                "captured_at": int(time.time()) if captured_at is None else captured_at,
                "limits": limits,
            }
        )
    except ValueError:
        return None
