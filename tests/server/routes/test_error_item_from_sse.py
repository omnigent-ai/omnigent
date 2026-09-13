"""Tests for ``_error_item_from_sse`` source attribution.

Verify that ``response.failed`` events carry through the ``source`` field from
the runner so downstream stores the right ``ErrorData.source`` (llm vs execution).
"""

from __future__ import annotations

from omnigent.entities.conversation import ErrorData
from omnigent.server.routes._sessions.helpers import _error_item_from_sse


def _failed_event(
    *,
    source: str | None = None,
    code: str = "connection_error",
    message: str = "oops",
    response_id: str = "resp_1",
) -> dict[str, object]:
    """Build a minimal ``response.failed`` SSE event dict."""
    error: dict[str, object] = {"code": code, "message": message}
    response: dict[str, object] = {"id": response_id, "status": "failed", "error": error}
    event: dict[str, object] = {
        "type": "response.failed",
        "response": response,
        "error": error,
    }
    if source is not None:
        event["source"] = source
    return event


def _error_event(
    *,
    source: str = "llm",
    code: str = "rate_limit",
    message: str = "Too many requests",
) -> dict[str, object]:
    """Build a minimal ``response.error`` SSE event dict."""
    return {
        "type": "response.error",
        "source": source,
        "error": {"code": code, "message": message},
    }


def test_response_failed_defaults_source_to_execution() -> None:
    """A ``response.failed`` without a ``source`` field falls back to ``execution``."""
    item = _error_item_from_sse(_failed_event(), response_id="resp_1")
    assert item is not None
    assert isinstance(item.data, ErrorData)
    assert item.data.source == "execution"


def test_response_failed_propagates_llm_source() -> None:
    """A ``response.failed`` with ``source="llm"`` (e.g. context overflow) is
    stored as ``source="llm"`` so users can see it's an inference fault."""
    item = _error_item_from_sse(
        _failed_event(source="llm", code="context_length_exceeded", message="too long"),
        response_id="resp_1",
    )
    assert item is not None
    assert isinstance(item.data, ErrorData)
    assert item.data.source == "llm"


def test_response_failed_propagates_harness_source() -> None:
    """A ``response.failed`` with ``source="harness"`` (Claude Code crash) is stored
    as ``source="harness"`` so users can see it's a harness fault, not Omnigent."""
    item = _error_item_from_sse(
        _failed_event(source="harness", code="connection_error", message="harness died"),
        response_id="resp_1",
    )
    assert item is not None
    assert isinstance(item.data, ErrorData)
    assert item.data.source == "harness"


def test_response_failed_propagates_execution_source_explicitly() -> None:
    """Explicit ``source="execution"`` is preserved (same as default but asserted)."""
    item = _error_item_from_sse(
        _failed_event(source="execution"),
        response_id="resp_1",
    )
    assert item is not None
    assert isinstance(item.data, ErrorData)
    assert item.data.source == "execution"


def test_response_error_source_preserved() -> None:
    """``response.error`` events already carry explicit ``source``; verify preserved."""
    item = _error_item_from_sse(_error_event(source="tool"), response_id="resp_1")
    assert item is not None
    assert isinstance(item.data, ErrorData)
    assert item.data.source == "tool"


def test_response_failed_no_response_id_returns_none() -> None:
    """``response.failed`` without response_id and no id in response → not persisted."""
    event = {
        "type": "response.failed",
        "source": "llm",
        "response": {"status": "failed", "error": {"code": "err", "message": "msg"}},
        "error": {"code": "err", "message": "msg"},
    }
    item = _error_item_from_sse(event, response_id=None)
    assert item is None
