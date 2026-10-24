"""Shared read-only protocol for server-owned remote child dispatch receipts."""

from __future__ import annotations

import json
import re
from typing import Any

REMOTE_CHILD_NAMESPACE = "omnigent.remote_child."
REMOTE_CHILD_STATE = f"{REMOTE_CHILD_NAMESPACE}state"
SUBAGENT_DELIVERED_ID_LABEL_KEY = "omnigent.subagent.delivered_id"
TERMINAL = frozenset({"completed", "failed", "cancelled"})


def remote_child_state(labels: dict[str, str]) -> dict[str, Any] | None:
    """Return only a structurally valid receipt, never inferred completion."""
    raw = labels.get(REMOTE_CHILD_STATE)
    if raw is None:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    dispatch_id = state.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", dispatch_id):
        return None
    status = state.get("status")
    if not isinstance(status, str) or status not in {"pending", "running", *TERMINAL}:
        return None
    response_id = state.get("response_id")
    if response_id is not None and (
        not isinstance(response_id, str) or not re.fullmatch(r"[0-9a-f]{64}", response_id)
    ):
        return None
    if status in {"running", "completed", "failed"} and response_id is None:
        return None
    item_id = state.get("result_item_id")
    if item_id is not None and not isinstance(item_id, str):
        return None
    return state
