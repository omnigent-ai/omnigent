"""Unit tests for the canonical sub-agent busy predicate
(:mod:`omnigent_client._child_status`).

This predicate is the single source of truth shared by the CLI ``state: N
agents running`` badge (via the terminal host) and the SDK rollups
(``subtree_busy`` / ``tree_busy``). The cases below pin the web-parity
semantics so the CLI and SDK can't drift: awaiting-input outranks everything,
``launching`` and the live ``busy`` flag count, non-terminal task statuses
count, and terminal/idle states do not. A regression here silently changes
when a driver decides "nothing is working" — exactly the bug #444 reports.
"""

from __future__ import annotations

import pytest
from omnigent_client import child_session_busy, child_summary_busy


@pytest.mark.parametrize(
    ("busy", "status", "pending", "expected"),
    [
        # Awaiting input outranks everything (even a terminal status / not busy).
        (False, "completed", 1, True),
        (False, None, 2, True),
        # Launching: spawned, not yet running.
        (False, "launching", 0, True),
        # Live busy flag.
        (True, None, 0, True),
        (True, "completed", 0, True),  # busy beats a stale completed
        # Non-terminal task status with no busy flag (busy-gap between turns).
        (False, "in_progress", 0, True),
        (False, "queued", 0, True),
        # Terminal statuses → not working.
        (False, "completed", 0, False),
        (False, "failed", 0, False),
        (False, "cancelled", 0, False),
        # Warm-idle: loop idle, no task, no pending input.
        (False, None, 0, False),
    ],
)
def test_child_session_busy_matrix(
    busy: bool, status: str | None, pending: int, expected: bool
) -> None:
    assert (
        child_session_busy(
            busy=busy,
            current_task_status=status,
            pending_elicitations_count=pending,
        )
        is expected
    )


def test_pending_defaults_to_zero() -> None:
    # The kwarg is optional; omitting it must not flip an idle child to busy.
    assert child_session_busy(busy=False, current_task_status=None) is False
    assert child_session_busy(busy=False, current_task_status="completed") is False


def test_child_summary_busy_reads_dict_fields() -> None:
    assert child_summary_busy(
        {"busy": True, "current_task_status": "completed", "pending_elicitations_count": 0}
    )
    assert not child_summary_busy(
        {"busy": False, "current_task_status": "failed", "pending_elicitations_count": 0}
    )
    assert child_summary_busy(
        {"busy": False, "current_task_status": "completed", "pending_elicitations_count": 3}
    )


def test_child_summary_busy_tolerates_missing_and_none_fields() -> None:
    # A poll row that hasn't run a task yet: no busy, null status, no count key.
    assert child_summary_busy({}) is False
    assert child_summary_busy({"current_task_status": None}) is False
    assert child_summary_busy({"pending_elicitations_count": None, "busy": None}) is False
    # Only a non-terminal status present → busy.
    assert child_summary_busy({"current_task_status": "in_progress"}) is True
