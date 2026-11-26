"""JSON payloads carried in workspace API messages as ``google.protobuf.Struct``.

The pull request methods answer with the REST routes' JSON payloads, so a client
reads either route alike. A Struct keeps every number as a double; whole numbers
come back as ``int`` so the payload reads as the REST route's would.
"""

from __future__ import annotations

import json
from typing import Any

from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct


def payload_struct(payload: dict[str, Any]) -> Struct:
    """Return ``payload`` as a Struct, as the REST route would serialize it to JSON."""
    struct = Struct()
    # The JSON round trip turns tuples and other JSON-encodable values into plain lists.
    struct.update(json.loads(json.dumps(payload)))
    return struct


def payload_dict(struct: Struct) -> dict[str, Any]:
    """Return a Struct's payload, with whole numbers as ``int``."""
    return _whole_numbers(json_format.MessageToDict(struct))


def _whole_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _whole_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_whole_numbers(item) for item in value]
    return value
