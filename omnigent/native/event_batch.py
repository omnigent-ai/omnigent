"""Wire contract for byte-bounded native transcript item batches."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

EXTERNAL_CONVERSATION_ITEM_BATCH_TYPE = "external_conversation_item_batch"

# Limit the encoded JSON request body itself, not just the sum of item text.
# The count bound also keeps validation, persistence, and SSE fan-out predictable.
MAX_EXTERNAL_ITEM_BATCH_BYTES = 1024 * 1024
MAX_EXTERNAL_ITEM_BATCH_ITEMS = 100


def external_item_batch_payload(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Build the canonical event envelope for one native item batch."""
    return {
        "type": EXTERNAL_CONVERSATION_ITEM_BATCH_TYPE,
        "data": {"items": list(items)},
    }


def encode_external_item_batch(items: Sequence[dict[str, Any]]) -> bytes:
    """Encode a batch exactly as it will be sent over HTTP."""
    return json.dumps(
        external_item_batch_payload(items),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
