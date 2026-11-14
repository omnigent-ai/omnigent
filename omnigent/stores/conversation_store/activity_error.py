"""Latest-activity-error label: the sidebar error badge, maintained on append.

The store folds each appended batch into :data:`LATEST_ACTIVITY_ERROR_LABEL_KEY`
so the session list carries the badge without reading history. The value is:

- ``"error"`` when the newest non-info error comes after the newest assistant
  output, or the newest assistant text starts with ``API Error:``;
- ``"disconnected"`` when that newest error is a runner disconnect;
- ``""`` otherwise.

User, meta, compaction, resource and info-level items never change it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, Protocol

from omnigent.entities import ErrorData, MessageData

LATEST_ACTIVITY_ERROR_LABEL_KEY = "omnigent.latest_activity_error"

ActivityError = Literal["error", "disconnected", ""]

_PENDING_INPUT_RESPONSE_ID = "pending_input"
_API_ERROR_PREFIX = "API Error:"
# Tool calls and their results are assistant output; they clear an earlier error.
_TOOL_ACTIVITY_TYPES = frozenset({"function_call", "function_call_output"})


class StoredItem(Protocol):
    """The fields of a new or persisted conversation item the fold reads."""

    @property
    def type(self) -> str: ...

    @property
    def response_id(self) -> str: ...

    @property
    def data(self) -> Any: ...


def fold_activity_error(batch: Iterable[StoredItem]) -> ActivityError | None:
    """Return the label value after appending ``batch``, or ``None`` to keep it.

    ``batch`` is in transcript order and newer than all stored history, so its
    last decisive item (a non-info error or assistant output) decides the value
    alone. A batch with no decisive item leaves the stored value as it is.

    :param batch: Newly persisted items, oldest first.
    :returns: The new value, or ``None`` when the batch decides nothing.
    """
    value: ActivityError | None = None
    for item in batch:
        if item.response_id == _PENDING_INPUT_RESPONSE_ID:
            continue
        data = item.data
        if item.type == "error" and isinstance(data, ErrorData):
            if data.level != "info":
                value = "disconnected" if data.code == "runner_disconnected" else "error"
        elif item.type == "message" and isinstance(data, MessageData):
            if data.role == "assistant" and not data.is_meta:
                text = "".join(
                    t
                    for b in data.content
                    if b.get("type") == "output_text" and isinstance(t := b.get("text"), str)
                )
                value = "error" if text.lstrip().startswith(_API_ERROR_PREFIX) else ""
        elif item.type in _TOOL_ACTIVITY_TYPES:
            value = ""
    return value
