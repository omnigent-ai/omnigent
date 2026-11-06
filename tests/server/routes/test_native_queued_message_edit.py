"""Editing a claude-native queued message from the web, and from the pane."""

import json
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
    monkeypatch: pytest.MonkeyPatch,
    *,
    harness: str,
    runner_status: int | None,
    runner_reply: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    forwarded: list[dict[str, Any]] = []

    async def forward(
        _session_id: str, _runner_router: object, event: dict[str, Any], **_kwargs: Any
    ) -> SimpleNamespace | None:
        forwarded.append(event)
        if runner_status is None:
            return None
        return SimpleNamespace(status_code=runner_status, body=json.dumps(runner_reply or {}))

    monkeypatch.setattr(sessions_facade, "_forward_session_change_to_runner", forward)
    monkeypatch.setattr(routes_events, "_native_pane_harness", lambda *_args, **_kwargs: harness)
    return forwarded


async def _edit(pending_id: str, content: object) -> None:
    await routes_events._edit_queued_native_message(
        _SESSION_ID,
        SimpleNamespace(),  # type: ignore[arg-type]
        {"pending_id": pending_id, "content": content},
        author=None,
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
    ("runner_status", "runner_reply", "error_code", "message"),
    [
        (409, {"error": "queued_message_missing"}, ErrorCode.CONFLICT, "no longer queued"),
        (
            503,
            {
                "error": "claude_native_queued_message_edit_failed",
                "detail": "Answer the pending Claude question first.",
            },
            ErrorCode.RUNNER_UNAVAILABLE,
            "Answer the pending Claude question first.",
        ),
        (
            409,
            {"error": "queued_message_ambiguous"},
            ErrorCode.CONFLICT,
            "Another queued message has the same text",
        ),
        (
            409,
            {
                "error": "no_live_harness",
                "detail": "no harness subprocess is running for this conversation",
            },
            ErrorCode.RUNNER_CAPABILITY_MISMATCH,
            "This session's runner can't edit queued messages.",
        ),
        (
            422,
            {"detail": "Input should be 'message', 'interrupt' or 'approval'"},
            ErrorCode.RUNNER_CAPABILITY_MISMATCH,
            "This session's runner can't edit queued messages.",
        ),
        (
            400,
            {"error": "invalid_input", "detail": "Body 'original' must be a string"},
            ErrorCode.RUNNER_UNAVAILABLE,
            "Couldn't edit the queued message in the terminal.",
        ),
        (
            204,
            {},
            ErrorCode.RUNNER_UNAVAILABLE,
            "Couldn't edit the queued message in the terminal.",
        ),
    ],
    ids=[
        "no-longer-queued",
        "pane-refused",
        "same-text-in-pane",
        "old-runner-without-harness",
        "old-runner-harness-rejected",
        "runner-rejected",
        "runner-without-the-session",
    ],
)
async def test_refused_web_edit_keeps_the_queued_message(
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    runner_status: int,
    runner_reply: dict[str, str],
    error_code: ErrorCode,
    message: str,
) -> None:
    forwarded = _route_to_runner(
        monkeypatch,
        harness="claude-native",
        runner_status=runner_status,
        runner_reply=runner_reply,
    )
    pending_id = pending_inputs.record(
        _SESSION_ID, [{"type": "input_text", "text": "Then summarize"}]
    )

    with pytest.raises(OmnigentError) as raised:
        await _edit(pending_id, "Then summarize the logs")
    with pytest.raises(OmnigentError) as retried:
        await _edit(pending_id, "Then summarize the logs")

    assert raised.value.code == error_code
    assert message in raised.value.message
    assert "subprocess" not in raised.value.message
    assert "must be a string" not in raised.value.message
    assert "Input should be" not in raised.value.message
    # A refusal settles the edit, so retrying reaches the runner again.
    assert retried.value.code == error_code
    assert len(forwarded) == 2
    assert _pending_texts() == ["Then summarize"]
    assert published == []
    # The refused edit leaves the message editable from the terminal.
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
    ("runner_status", "runner_reply", "message"),
    [
        (None, None, "may still apply"),
        (
            503,
            {
                "error": "claude_native_queued_message_edit_incomplete",
                "detail": 'Claude Code could not queue 1 message(s) again: "Last". Resend them.',
            },
            'Claude Code could not queue 1 message(s) again: "Last". Resend them.',
        ),
    ],
    ids=["unconfirmed", "prompts-unqueued"],
)
async def test_unconfirmed_web_edit_still_applies_to_its_own_message(
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    runner_status: int | None,
    runner_reply: dict[str, str] | None,
    message: str,
) -> None:
    forwarded = _route_to_runner(
        monkeypatch,
        harness="claude-native",
        runner_status=runner_status,
        runner_reply=runner_reply,
    )
    pending_inputs.record(_SESSION_ID, [{"type": "input_text", "text": "First"}])
    pending_id = pending_inputs.record(
        _SESSION_ID, [{"type": "input_text", "text": "Then summarize"}]
    )

    with pytest.raises(OmnigentError) as raised:
        await _edit(pending_id, "Then summarize the logs")
    with pytest.raises(OmnigentError) as overlapping:
        await _edit(pending_id, "Then summarize everything")

    assert raised.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert message in raised.value.message
    assert overlapping.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert len(forwarded) == 1
    # The pane's late records still give the edit to this message, not the oldest popped one.
    edits = pending_inputs.apply_queue_operations(
        _SESSION_ID,
        [
            pending_inputs.QueueOperation("popAll", "First", "t1"),
            pending_inputs.QueueOperation("popAll", "Then summarize", "t1"),
            pending_inputs.QueueOperation("enqueue", "First", "t2"),
            pending_inputs.QueueOperation("enqueue", "Then summarize the logs", "t3"),
        ],
    )
    assert [(edit.pending_id, edit.merged_pending_ids) for edit in edits] == [(pending_id, [])]
    assert _pending_texts() == ["First", "Then summarize the logs"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "queue", "content", "error_code", "refusal"),
    [
        ("codex-native", "queued", "edited", ErrorCode.INVALID_INPUT, "Claude Code sessions"),
        ("claude-native", "queued", "  ", ErrorCode.INVALID_INPUT, "non-empty data.content"),
        ("claude-native", "queued", ["edited"], ErrorCode.INVALID_INPUT, "non-empty data.content"),
        ("claude-native", "consumed", "edited", ErrorCode.CONFLICT, "no longer queued"),
        ("claude-native", "queued-twice", "edited", ErrorCode.CONFLICT, "same text"),
    ],
    ids=["other-harness", "blank-text", "non-string-text", "already-consumed", "same-text"],
)
async def test_web_edit_is_refused_before_reaching_the_runner(
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    harness: str,
    queue: str,
    content: object,
    error_code: ErrorCode,
    refusal: str,
) -> None:
    forwarded = _route_to_runner(monkeypatch, harness=harness, runner_status=200)
    pending_id = pending_inputs.record(_SESSION_ID, [{"type": "input_text", "text": "original"}])
    if queue == "consumed":
        pending_inputs.resolve_matching_text(_SESSION_ID, "original")
    elif queue == "queued-twice":
        pending_id = pending_inputs.record(
            _SESSION_ID, [{"type": "input_text", "text": "original"}]
        )
    texts = _pending_texts()

    with pytest.raises(OmnigentError) as raised:
        await _edit(pending_id, content)

    assert raised.value.code == error_code
    assert refusal in raised.value.message
    assert forwarded == []
    assert published == []
    assert _pending_texts() == texts


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
