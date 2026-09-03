"""Unit tests for the child-session summary's live ``status`` projection.

``_child_session_summary_from_conversation`` derives ``busy`` /
``current_task_status`` / ``status`` from the relay-fed status cache. A
cache miss — a replica that doesn't hold the runner tunnel, or a server
that restarted after the status was published — must fall back to the
row's durable ``live_status`` instead of presenting the child as unknown:
that blank is what left an orchestrating parent unable to tell a child
whose turn died with an interrupted runner from one still working.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.common import (
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
)
from omnigent.server.routes._sessions.helpers import (
    _child_session_summary_from_conversation,
    _session_status_cache,
)


def _child(
    *,
    live_status: str | None = None,
    labels: dict[str, str] | None = None,
) -> Conversation:
    """A minimal sub-agent conversation row.

    :param live_status: The durable relay-persisted turn status on the row.
    :param labels: Guardrails labels (e.g. a durable ``last_task_error``).
    :returns: The conversation entity.
    """
    return Conversation(
        id="conv_child",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_parent",
        title="researcher:auth",
        agent_id="ag_test",
        labels=labels or {},
        live_status=live_status,
    )


@pytest.fixture(autouse=True)
def _clean_status_cache() -> Iterator[None]:
    """Isolate the module-global relay status cache around each test."""
    saved = dict(_session_status_cache)
    _session_status_cache.clear()
    yield
    _session_status_cache.clear()
    _session_status_cache.update(saved)


def test_cache_miss_falls_back_to_durable_live_status() -> None:
    """No cache entry: the row's persisted ``live_status`` is the status."""
    summary = _child_session_summary_from_conversation(
        _child(live_status="idle"), "conv_parent", None
    )
    assert summary.status == "idle"
    assert summary.busy is False
    assert summary.current_task_status == "completed"


def test_cache_entry_wins_over_the_row() -> None:
    """The live cache (fresher than the row write) takes precedence."""
    _session_status_cache["conv_child"] = "running"
    summary = _child_session_summary_from_conversation(
        _child(live_status="idle"), "conv_parent", None
    )
    assert summary.status == "running"
    assert summary.busy is True


def test_waiting_and_failed_surface_verbatim() -> None:
    """The fine-grained statuses pass through unchanged."""
    _session_status_cache["conv_child"] = "waiting"
    waiting = _child_session_summary_from_conversation(_child(), "conv_parent", None)
    assert waiting.status == "waiting"
    assert waiting.busy is True

    _session_status_cache["conv_child"] = "failed"
    failed = _child_session_summary_from_conversation(_child(), "conv_parent", None)
    assert failed.status == "failed"
    assert failed.busy is False


def test_no_state_anywhere_is_none() -> None:
    """A child that never reported a status has ``status=None``, not a guess."""
    summary = _child_session_summary_from_conversation(_child(), "conv_parent", None)
    assert summary.status is None
    assert summary.busy is False
    assert summary.current_task_status is None


def test_durable_task_error_forces_failed() -> None:
    """A durable ``last_task_error`` overrides a stale idle status."""
    labels = {
        _LAST_TASK_ERROR_CODE_LABEL_KEY: "runner_disconnected",
        _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "Runner disconnected unexpectedly.",
    }
    summary = _child_session_summary_from_conversation(
        _child(live_status="idle", labels=labels), "conv_parent", None
    )
    assert summary.status == "failed"
    assert summary.current_task_status == "failed"
