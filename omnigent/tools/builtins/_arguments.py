"""Argument parsing helpers for built-in tools."""

from __future__ import annotations

import json
from typing import Any


def parse_json_object_arguments(
    arguments: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Parse raw tool arguments as a JSON object.

    Empty arguments are treated as ``{}``, so required-argument tools can
    return their normal missing-field errors instead of raising from
    ``json.loads``.
    """
    if not arguments.strip():
        return {}, None
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None, "malformed JSON arguments"
    if not isinstance(parsed, dict):
        return None, "arguments must be a JSON object"
    return parsed, None
