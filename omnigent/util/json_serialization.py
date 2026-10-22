"""JSON serialization helpers for UTF-8 transport boundaries."""

from __future__ import annotations

import json


def json_dumps_transport_safe(value: object) -> str:
    """Serialize readable Unicode while escaping lone surrogate code points."""
    serialized = json.dumps(value, ensure_ascii=False)
    return serialized.encode("utf-8", errors="backslashreplace").decode("utf-8")
