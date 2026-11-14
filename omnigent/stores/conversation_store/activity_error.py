"""Latest-activity-error label: the sidebar error badge, maintained on append.

The store folds each appended batch into :data:`LATEST_ACTIVITY_ERROR_LABEL_KEY`
so the session list carries the badge without reading history. The value is:

- ``"error"`` when the newest non-info error comes after the newest assistant
  output, or the newest assistant text starts with ``API Error:``;
- ``"disconnected"`` for a runner disconnect without a same-response fault;
- ``""`` otherwise.

:data:`LATEST_ACTIVITY_ERROR_RESPONSE_LABEL_KEY` records the response that set
the value, so a disconnect later in that response cannot erase a genuine fault.
User, meta, compaction, resource and info-level items never change either label.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, NamedTuple, Protocol

from omnigent.entities import ErrorData, MessageData

LATEST_ACTIVITY_ERROR_LABEL_KEY = "omnigent.latest_activity_error"
LATEST_ACTIVITY_ERROR_RESPONSE_LABEL_KEY = "omnigent.latest_activity_error_response"

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


class ActivityErrorSummary(NamedTuple):
    """Both stored labels: the value and the response that set it (``""`` when clear)."""

    value: ActivityError
    response_id: str = ""


def fold_activity_error(
    batch: Iterable[StoredItem], previous: ActivityErrorSummary | None = None
) -> ActivityErrorSummary | None:
    """Return the summary after appending ``batch``, or ``None`` to keep it.

    ``batch`` is newer than all stored history, so its last decisive item (a
    non-info error or assistant output) decides alone, except that a disconnect
    keeps a genuine fault from the same response.

    :param batch: Newly persisted items, oldest first.
    :param previous: The stored summary, consulted only for that exception.
    :returns: The new summary, or ``None`` when the batch decides nothing.
    """
    summary = previous
    changed = False
    for item in batch:
        if item.response_id == _PENDING_INPUT_RESPONSE_ID:
            continue
        data = item.data
        if item.type == "error" and isinstance(data, ErrorData):
            if data.level != "info":
                disconnect = data.code == "runner_disconnected"
                if not (
                    disconnect
                    and summary is not None
                    and summary.value == "error"
                    and summary.response_id == item.response_id
                ):
                    summary = ActivityErrorSummary(
                        "disconnected" if disconnect else "error", item.response_id
                    )
                changed = True
        elif item.type == "message" and isinstance(data, MessageData):
            if data.role == "assistant" and not data.is_meta:
                text = "".join(
                    t
                    for b in data.content
                    if b.get("type") == "output_text" and isinstance(t := b.get("text"), str)
                )
                fault = text.lstrip().startswith(_API_ERROR_PREFIX)
                summary = (
                    ActivityErrorSummary("error", item.response_id)
                    if fault
                    else ActivityErrorSummary("")
                )
                changed = True
        elif item.type in _TOOL_ACTIVITY_TYPES:
            summary = ActivityErrorSummary("")
            changed = True
    return summary if changed else None
