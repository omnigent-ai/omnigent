"""Elicitation entity — persisted in the ``elicitations`` table.

One row per approval prompt that is still waiting on a human. The row exists
only while the prompt is outstanding: resolving it deletes the row, so the
table holds the parked set and nothing else. Durable history of *answered*
approvals is not this table's job.

The row stores the whole ``response.elicitation_request`` event rather than its
parts, because that event is exactly what
``GET /v1/sessions/{id}`` replays into the UI — see
:func:`omnigent.runtime.pending_elicitations.snapshot_for`. Keeping it opaque
means a new field on the event needs no migration here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Elicitation:
    """
    One outstanding approval prompt.

    :param id: Correlation id the prompt was minted with, e.g.
        ``"elicit_abc123"``. Not a UUID — several native harnesses derive
        deterministic ids from the session and the gated tool call (see
        ``cursor_tool_call_elicitation_id``), so this is an opaque string.
    :param workspace_id: Tenant partition key that owns this row.
    :param conversation_id: Session the prompt was raised on, e.g.
        ``"conv_abc123"``.
    :param created_at: Unix epoch seconds the prompt was raised.
    :param event: The ``response.elicitation_request`` event payload, verbatim.
    """

    id: str
    workspace_id: int
    conversation_id: str
    created_at: int
    event: dict[str, Any]
