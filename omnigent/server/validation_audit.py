"""Bounded schema diagnostics for the request audit row."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import get_args

from pydantic_core.core_schema import ErrorType

_ERROR_TYPES = frozenset(get_args(ErrorType))
_MAX_ERROR_SAMPLES = 8
_MAX_LOCATION_DEPTH = 12
_LOCATION_FIELDS = frozenset(
    {
        "body",
        "query",
        "path",
        "header",
        "type",
        "data",
        "content",
        "role",
        "text",
        "items",
        "item_type",
        "item_data",
        "source_id",
        "response_id",
        "message_id",
        "output",
        "arguments",
        "call_id",
        "name",
        "id",
        "status",
        "image_url",
        "file_id",
        "model_override",
        "usage",
    }
)


def validation_audit_attrs(errors: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Keep built-in error types and schema fields; discard values and dynamic keys.

    Location tuples are filtered through _LOCATION_FIELDS: any string matching a
    known field name is retained, others become a generic "field" marker. Caller-
    controlled keys (e.g. in dict fields) survive if they happen to match an
    allowlisted field name, but all values are omitted. This bounds the logged
    output to a fixed vocabulary without attempting a schema introspection.
    """
    samples = errors[:_MAX_ERROR_SAMPLES]
    types: set[str] = set()
    locations: list[list[str]] = []
    for error in samples:
        kind = error.get("type")
        types.add(kind if isinstance(kind, str) and kind in _ERROR_TYPES else "custom")
        location = error.get("loc")
        parts = location[:_MAX_LOCATION_DEPTH] if isinstance(location, (tuple, list)) else []
        locations.append(
            [
                part
                if isinstance(part, str) and part in _LOCATION_FIELDS
                else "[]"
                if isinstance(part, int) and not isinstance(part, bool)
                else "field"
                for part in parts
            ]
        )
    return {
        "validation_error_count": str(len(errors)),
        "validation_error_types": json.dumps(sorted(types), separators=(",", ":")),
        "validation_error_locations": json.dumps(locations, separators=(",", ":")),
    }
