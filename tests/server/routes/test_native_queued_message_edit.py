"""Editing a claude-native queued message from the web, and from the pane."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_inputs, session_stream
from omnigent.server.routes import sessions as sessions_facade
from omnigent.server.routes.sessions import routes_events

_SESSION_ID = "64a784c3aa907d1774f44313546947c6"


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_stream, "publish", lambda _session_id, event: events.append(event))
    pending_inputs.reset_for_tests()
    yield events
    pending_inputs.reset_for_tests()


def _route_to_runner(
    monkeypatch: pytest.MonkeyPatch, *, harness: str, runner_status: int | None
) -> list[dict[str, Any]]:
    forwarded: list[dict[str, Any]] = []

    async def forward(
        _session_id: str, _runner_router: object, event: dict[str, Any], **_kwargs: Any
    ) -> SimpleNamespace | None:
        forwarded.append(event)
        return None if runner_status is None else SimpleNamespace(status_code=runner_status)

    monkeypatch.setattr(sessions_facade, "_forward_session_change_to_runner", forward)
    monkeypatch.setattr(routes_events, "_native_pane_harness", lambda *_args, **_kwargs: harness)
    return forwarded


async def _edit(pending_id: str, content: object) -> None:
    await routes_events._edit_queued_native_message(
        _SESSION_ID,
        SimpleNamespace(),  # type: ignore[arg-type]
        {"pending_id": pending_id, "content": content},
        runner_router=None,
        agent_store=SimpleNamespace(),  # type: ignore[arg-type]
        agent_cache=None,
    )


def _pending_texts() -> list[str]:
    return [
        block["text"]
        for entry in pending_inputs.snapshot_for(_SESSION_ID)
        for block in entry["content"]
    ]


@pytest.mark.asyncio
async def test_confirmed_web_edit_updates_the_queued_message_and_publishes_it(
    monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    forwarded = _route_to_runner(monkeypatch, harness="claude-native", runner_status=200)
    pending_id = pending_inputs.record(
        _SESSION_ID, [{"type": "input_text", "text": "Then summarize"}]
    )

    await _edit(pending_id, "Then summarize the logs")

    assert forwarded == [
        {
            "type": "queued_message_edit",
            "original": "Then summarize",
            "replacement": "Then summarize the logs",
        }
    ]
    assert _pending_texts() == ["Then summarize the logs"]
    assert [(event["type"], event["data"]) for event in published] == [
        (
            "session.input.edited",
            {
                "pending_id": pending_id,
                "content": [{"type": "input_text", "text": "Then summarize the logs"}],
                "merged_pending_ids": [],
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_status", "error_code"),
    [
        (409, ErrorCode.CONFLICT),
        (503, ErrorCode.RUNNER_UNAVAILABLE),
        (None, ErrorCode.RUNNER_UNAVAILABLE),
    ],
    ids=["no-longer-queued", "pane-failed", "runner-unreachable"],
)
async def test_failed_web_edit_keeps_the_queued_message(
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    runner_status: int | None,
    error_code: ErrorCode,
) -> None:
    _route_to_runner(monkeypatch, harness="claude-native", runner_status=runner_status)
    pending_id = pending_inputs.record(
        _SESSION_ID, [{"type": "input_text", "text": "Then summarize"}]
    )

    with pytest.raises(OmnigentError) as raised:
        await _edit(pending_id, "Then summarize the logs")

    assert raised.value.code == error_code
    assert _pending_texts() == ["Then summarize"]
    assert published == []
    # The cancelled plan no longer redirects the pane's own edit of this message.
    edits = pending_inputs.apply_queue_operations(
        _SESSION_ID,
        [
            pending_inputs.QueueOperation("popAll", "Then summarize", "t1"),
            pending_inputs.QueueOperation("enqueue", "Then summarize in the TUI", "t2"),
        ],
    )
    assert [edit.content[0]["text"] for edit in edits] == ["Then summarize in the TUI"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "pending", "content", "error_code"),
    [
        ("codex-native", True, "edited", ErrorCode.INVALID_INPUT),
        ("claude-native", True, "  ", ErrorCode.INVALID_INPUT),
        ("claude-native", True, ["edited"], ErrorCode.INVALID_INPUT),
        ("claude-native", False, "edited", ErrorCode.CONFLICT),
    ],
    ids=["other-harness", "blank-text", "non-string-text", "already-consumed"],
)
async def test_web_edit_is_refused_before_reaching_the_runner(
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    harness: str,
    pending: bool,
    content: object,
    error_code: ErrorCode,
) -> None:
    forwarded = _route_to_runner(monkeypatch, harness=harness, runner_status=200)
    pending_id = pending_inputs.record(_SESSION_ID, [{"type": "input_text", "text": "original"}])
    if not pending:
        pending_inputs.resolve_matching_text(_SESSION_ID, "original")

    with pytest.raises(OmnigentError) as raised:
        await _edit(pending_id, content)

    assert raised.value.code == error_code
    assert forwarded == []
    assert published == []


@pytest.mark.parametrize(
    "operations",
    [None, [{"operation": "popAll"}], [{"operation": "popAll", "content": "x", "timestamp": 1}]],
    ids=["missing-list", "missing-content", "non-string-timestamp"],
)
def test_malformed_queue_operations_are_rejected(operations: object) -> None:
    with pytest.raises(OmnigentError) as raised:
        routes_events._queue_operations_from_event({"operations": operations})

    assert raised.value.code == ErrorCode.INVALID_INPUT


def test_queue_operations_parse_in_order_with_an_optional_timestamp() -> None:
    parsed = routes_events._queue_operations_from_event(
        {
            "operations": [
                {"operation": "popAll", "content": "Then summarize", "timestamp": "t1"},
                {"operation": "enqueue", "content": "Then summarize the logs"},
            ]
        }
    )

    assert parsed == [
        pending_inputs.QueueOperation("popAll", "Then summarize", "t1"),
        pending_inputs.QueueOperation("enqueue", "Then summarize the logs", ""),
    ]
