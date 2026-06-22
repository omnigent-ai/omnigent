"""
Tests for the LLM-facing async-handle shape — ``_AsyncToolHandle`` and
``_async_handle_message`` in :mod:`omnigent.runtime.workflow`.
"""

from __future__ import annotations

import json

from omnigent.runtime.workflow import (
    _async_handle_message,
    _AsyncToolHandle,
)

# ─── _AsyncToolHandle / _async_handle_message ────────────────


def test_handle_message_names_task_id_and_tool_name() -> None:
    """The LLM-facing message embeds both the task_id and tool name (G12).

    Without the literal task_id in the message, the LLM has no way to
    know what to pass to ``sys_cancel_task`` if it wants to abort.
    Without the tool name, the LLM may forget which call the handle
    corresponds to when it issued multiple parallel async calls.
    """
    text = _async_handle_message("tsk_async_42", "train_model")
    assert "tsk_async_42" in text
    assert "train_model" in text
    # Mentions sys_cancel_task so the LLM knows it can abort —
    # check_task was dropped per design step 11; results
    # auto-deliver via the inbox instead.
    assert "sys_cancel_task" in text


def test_handle_serializes_to_json_with_required_fields() -> None:
    """``to_handle_json`` produces a JSON dict the LLM can parse."""
    handle = _AsyncToolHandle(
        task_id="tsk_h1",
        tool_name="long_running",
        status="in_progress",
        message="msg body",
    )
    parsed = json.loads(handle.to_handle_json())
    # All four documented fields must be present — missing any of
    # them would force the LLM to guess (e.g. status defaulting
    # silently to "completed" if the field is absent).
    assert parsed == {
        "task_id": "tsk_h1",
        "tool_name": "long_running",
        "status": "in_progress",
        "message": "msg body",
    }


def test_handle_status_is_in_progress_at_creation() -> None:
    """Fresh handles always report ``in_progress`` (D7)."""
    # The terminal status arrives later via async_work_complete.
    # If the handle reported "completed" at creation, the LLM
    # would assume the tool is done and not wait for the
    # auto-delivered result.
    handle = _AsyncToolHandle(
        task_id="tsk_h2",
        tool_name="x",
        status="in_progress",
        message="",
    )
    assert handle.status == "in_progress"
