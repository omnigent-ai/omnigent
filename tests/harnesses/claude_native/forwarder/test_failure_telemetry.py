"""Native error evidence survives hooks, transcript parsing, retries and forwarding."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.harnesses.claude_native.failure_telemetry import claude_failure_context
from omnigent.native.failure_telemetry import FailureContext


@pytest.mark.parametrize(
    "scenario",
    [
        "current_error",
        "multiple_error_blocks",
        "recovered_turn",
        "failed_item_after_error",
        "failed_error_item",
        "later_turn",
        "different_response",
        "foreign_session",
        "subagent_error",
        "unknown_response",
        "unmarked_prose",
        "explicit_hook_detail",
        "missing_forward_state",
        "malformed_forward_state",
        "missing_native_session",
        "missing_error_text",
        "unmarked_retained_context",
    ],
)
@pytest.mark.asyncio
async def test_category_only_failure_retains_current_api_error_after_restart(
    tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    error_text = "Not logged in · Please run /login"
    records = [
        {
            "type": "user",
            "uuid": "current-user",
            "sessionId": "native-session",
            "message": {"role": "user", "content": "Continue"},
        },
        {
            "type": "assistant",
            "uuid": "current-error",
            "sessionId": "other-session" if scenario == "foreign_session" else "native-session",
            "isApiErrorMessage": scenario != "unmarked_prose",
            "message": {"role": "assistant", "content": error_text},
        },
    ]
    if scenario == "subagent_error":
        records[1]["agentId"] = "native-child"
    if scenario == "multiple_error_blocks":
        records[1]["message"] = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": error_text},
                {"type": "text", "text": "The current request failed."},
            ],
        }
    transcript_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    hook = {
        "hook_event_name": "StopFailure",
        "session_id": "native-session",
        "error": "authentication_failed",
        "last_assistant_message": "The earlier task succeeded.",
    }
    if scenario == "explicit_hook_detail":
        hook["error_details"] = "API Error: 401 Invalid token"
    if scenario == "missing_native_session":
        hook.pop("session_id")
    bridge.record_hook_event(bridge_dir, hook)
    failures = []

    def handle(request: httpx.Request) -> httpx.Response:
        event = json.loads(request.content)
        if event["type"] == "external_session_status":
            failures.append(event["data"])
            return httpx.Response(
                204
                if scenario in {"failed_item_after_error", "failed_error_item"}
                else (503 if len(failures) == 1 else 204)
            )
        source_id = event["data"]["source_id"]
        if (scenario == "failed_error_item" and "current-error" in source_id) or (
            scenario == "failed_item_after_error" and "later-record" in source_id
        ):
            return httpx.Response(503)
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        transcript_state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_synthetic",
            bridge_dir=bridge_dir,
            agent_name="test-agent",
            state=forwarder.TranscriptForwardState(transcript_path, 0),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
        )
        if scenario in {"recovered_turn", "failed_item_after_error", "later_turn"}:
            role = "user" if scenario == "later_turn" else "assistant"
            with transcript_path.open("a") as transcript:
                transcript.write(
                    json.dumps(
                        {
                            "type": role,
                            "uuid": "later-record",
                            "sessionId": "native-session",
                            "message": {"role": role, "content": "Later progress"},
                        }
                    )
                    + "\n"
                )
            transcript_state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_synthetic",
                bridge_dir=bridge_dir,
                agent_name="test-agent",
                state=transcript_state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
            )
        response_id = (
            None if scenario == "unknown_response" else transcript_state.current_response_id
        )
        if scenario == "different_response":
            response_id = "resp_different_turn"
        if scenario in {"failed_item_after_error", "failed_error_item"}:
            persisted = forwarder._read_forward_state(bridge_dir)
            assert persisted is not None
            assert persisted.api_error_context == transcript_state.api_error_context
            assert persisted.line_cursor == transcript_state.line_cursor
            failed_record = (
                "later-record" if scenario == "failed_item_after_error" else "current-error"
            )
            assert all(failed_record not in source_id for source_id in persisted.seen_source_ids)
        if scenario == "missing_forward_state":
            (bridge_dir / forwarder._FORWARDER_STATE_FILE).unlink()
        elif scenario == "malformed_forward_state":
            (bridge_dir / forwarder._FORWARDER_STATE_FILE).write_text("{invalid")
        elif scenario in {"missing_error_text", "unmarked_retained_context"}:
            retained = dict(transcript_state.api_error_context or {})
            if scenario == "missing_error_text":
                retained.pop("native_error_message")
            else:
                retained["native_api_error_message"] = False
            forwarder._write_forward_state(
                bridge_dir, replace(transcript_state, api_error_context=retained)
            )
        checkpoint_reads: list[Path] = []
        read_forward_state = forwarder._read_forward_state

        def read_checkpoint(path: Path) -> forwarder.TranscriptForwardState | None:
            checkpoint_reads.append(path)
            return read_forward_state(path)

        monkeypatch.setattr(forwarder, "_read_forward_state", read_checkpoint)
        for _ in range(1 if scenario in {"failed_item_after_error", "failed_error_item"} else 2):
            # Both retry attempts use fresh in-memory state; evidence must survive on disk.
            hook_state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_synthetic",
                bridge_dir=bridge_dir,
                state=forwarder.HookForwardState(event_cursor=0, byte_offset=0),
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id=response_id,
            )

    assert hook_state.event_cursor == 1
    assert len(failures) == (
        1 if scenario in {"failed_item_after_error", "failed_error_item"} else 2
    )
    if len(failures) == 2:
        assert failures[0] == failures[1]
    failure = failures[-1]
    context = failure["failure_context"]
    expected_context_state = {
        "explicit_hook_detail": "hook_details_present",
        "missing_native_session": "native_session_unavailable",
        "missing_forward_state": "forward_state_unavailable",
        "malformed_forward_state": "forward_state_unavailable",
        "different_response": "response_mismatch",
        "unknown_response": "response_mismatch",
        "foreign_session": "native_session_mismatch",
        "subagent_error": "subagent_context",
        "missing_error_text": "error_text_unavailable",
        "unmarked_retained_context": "unmarked_error_context",
        "recovered_turn": "no_api_error_context",
        "failed_item_after_error": "no_api_error_context",
        "unmarked_prose": "no_api_error_context",
    }.get(scenario, "matched")
    assert context["transcript_error_context_state"] == expected_context_state
    assert len(checkpoint_reads) == (
        0 if scenario in {"explicit_hook_detail", "missing_native_session"} else len(failures)
    )
    assert failure["status"] == "failed"
    assert failure.get("response_id") == response_id
    assert context["failure_source"] == "claude_hook"
    assert context["native_error_category"] == "authentication_failed"
    # A later prompt without output leaves the hook to end the failed turn.
    if scenario in {"current_error", "multiple_error_blocks", "failed_error_item", "later_turn"}:
        assert failure["failure_detail"].startswith(error_text + "\n\n")
        assert "omni setup" in failure["failure_detail"]
        assert context["detail_source"] == "explicit_api_error"
        assert context["native_api_error_message"] is True
        assert context["native_error_message"] == error_text
        assert context["native_record_id"] == "current-error"
    elif scenario == "explicit_hook_detail":
        assert failure["failure_detail"] == hook["error_details"]
        assert context["detail_source"] == "hook_error_details"
    else:
        assert failure["failure_detail"] == (
            "Claude Code ended the turn with an API error (authentication_failed)."
        )
        assert context["detail_source"] == "hook_error_category"
        assert "native_api_error_message" not in context


_SPEND_LIMIT_ERROR = (
    'API Error: 403 {"error":{"type":"permission_error",'
    '"message":"Synthetic spend limit reached"}}'
)
_MAIN_API_ERROR: dict[str, object] = {
    "type": "assistant",
    "uuid": "main-error",
    "isApiErrorMessage": True,
    "error": "authentication_failed",
    "message": {
        "role": "assistant",
        "model": "<synthetic>",
        "content": [{"type": "text", "text": _SPEND_LIMIT_ERROR}],
    },
}
_MAIN_STOP_FAILURE = {
    "hook_event_name": "StopFailure",
    "session_id": "native-session",
    "error": "authentication_failed",
}
_NEXT_TURN_RECORDS: dict[str, dict[str, object]] = {
    # Claude delivers a background agent's result right after the turn ends.
    "task_notification": {
        "type": "user",
        "uuid": "next-turn",
        "origin": {"kind": "task-notification"},
        "message": {
            "role": "user",
            "content": "<task-notification>\n<task-id>bg-agent</task-id>\n"
            "<status>failed</status>\n</task-notification>",
        },
    },
    # A prompt typed while the turn was running is submitted when it ends.
    "queued_prompt": {
        "type": "user",
        "uuid": "next-turn",
        "message": {"role": "user", "content": "Also update the docs"},
    },
}


def _append_transcript(path: Path, *records: dict[str, object]) -> None:
    with path.open("a") as transcript:
        for record in records:
            native = {"sessionId": "native-session", "isSidechain": False}
            transcript.write(json.dumps({**native, **record}) + "\n")


async def _poll_transcript(
    client: httpx.AsyncClient,
    bridge_dir: Path,
    state: forwarder.TranscriptForwardState,
    dedupe: forwarder._ForwardDedupeState,
) -> forwarder.TranscriptForwardState:
    return await forwarder._forward_available_items(
        client=client,
        session_id="conv_synthetic",
        bridge_dir=bridge_dir,
        agent_name="test-agent",
        state=state,
        retry_tracker=forwarder._PostRetryTracker(),
        dedupe=dedupe,
    )


async def _poll_hooks(
    client: httpx.AsyncClient,
    bridge_dir: Path,
    state: forwarder.HookForwardState,
    dedupe: forwarder._ForwardDedupeState,
    response_id: str | None,
    *,
    retry_tracker: forwarder._PostRetryTracker | None = None,
) -> forwarder.HookForwardState:
    return await forwarder._forward_available_status_events(
        client=client,
        session_id="conv_synthetic",
        bridge_dir=bridge_dir,
        state=state,
        retry_tracker=retry_tracker or forwarder._PostRetryTracker(),
        dedupe=dedupe,
        task_subjects={},
        task_statuses={},
        task_order=[],
        response_id=response_id,
    )


def _record_events(
    failures: list[dict[str, object]], error_posts: list[object] | None = None
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        event = json.loads(request.content)
        if event["type"] == "external_session_status":
            failures.append(event["data"])
            return httpx.Response(204)
        if error_posts is not None and "main-error" in event["data"]["source_id"]:
            error_posts.append(event["data"]["response_id"])
            # The first delivery of the API error fails and is retried.
            return httpx.Response(503 if len(error_posts) == 1 else 202, json={})
        return httpx.Response(202, json={})

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("next_turn", ["task_notification", "queued_prompt"])
@pytest.mark.parametrize("next_turn_poll", ["same_poll", "next_poll"])
@pytest.mark.asyncio
async def test_api_error_survives_next_turn_opening_before_stop_failure(
    tmp_path: Path, next_turn: str, next_turn_poll: str
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _append_transcript(
        transcript_path,
        {"type": "user", "uuid": "prompt", "message": {"role": "user", "content": "Run checks"}},
        {
            "type": "assistant",
            "uuid": "launch",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_bg", "name": "Agent", "input": {}}],
            },
        },
    )
    turn_end = {"type": "system", "subtype": "turn_duration", "uuid": "turn-duration"}
    failures: list[dict[str, object]] = []
    # One dedupe and transcript-derived response IDs, as in the forwarder loop.
    dedupe = forwarder._ForwardDedupeState()
    async with httpx.AsyncClient(
        transport=_record_events(failures), base_url="http://test"
    ) as client:
        state = await _poll_transcript(
            client, bridge_dir, forwarder.TranscriptForwardState(transcript_path, 0), dedupe
        )
        failed_response_id = state.current_response_id
        bridge.record_hook_event(bridge_dir, {**_MAIN_STOP_FAILURE, "agent_id": "bg-agent"})
        hooks = await _poll_hooks(
            client,
            bridge_dir,
            forwarder.HookForwardState(event_cursor=0, byte_offset=0),
            dedupe,
            state.current_response_id,
        )
        assert failures == []
        if next_turn_poll == "same_poll":
            _append_transcript(
                transcript_path, _MAIN_API_ERROR, turn_end, _NEXT_TURN_RECORDS[next_turn]
            )
        else:
            _append_transcript(transcript_path, _MAIN_API_ERROR, turn_end)
            state = await _poll_transcript(client, bridge_dir, state, dedupe)
            _append_transcript(transcript_path, _NEXT_TURN_RECORDS[next_turn])
        state = await _poll_transcript(client, bridge_dir, state, dedupe)
        # The next turn has opened, so the transcript no longer names the failed one.
        assert state.current_response_id != failed_response_id
        bridge.record_hook_event(bridge_dir, _MAIN_STOP_FAILURE)
        await _poll_hooks(client, bridge_dir, hooks, dedupe, state.current_response_id)

    (failure,) = failures
    assert failure["failure_detail"] == "Synthetic spend limit reached"
    context = failure["failure_context"]
    assert context["detail_source"] == "explicit_api_error"
    assert context["native_record_id"] == "main-error"
    assert context["native_agent_role"] == "session_agent"


@pytest.mark.asyncio
async def test_retried_api_error_keeps_its_turn_after_the_next_turn_opens(
    tmp_path: Path,
) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _append_transcript(
        transcript_path,
        {"type": "user", "uuid": "prompt", "message": {"role": "user", "content": "Run checks"}},
        {
            "type": "assistant",
            "uuid": "progress",
            "message": {"role": "assistant", "content": "On it"},
        },
    )
    failures: list[dict[str, object]] = []
    error_posts: list[object] = []
    dedupe = forwarder._ForwardDedupeState()
    async with httpx.AsyncClient(
        transport=_record_events(failures, error_posts), base_url="http://test"
    ) as client:
        state = await _poll_transcript(
            client, bridge_dir, forwarder.TranscriptForwardState(transcript_path, 0), dedupe
        )
        failed_response_id = state.current_response_id
        _append_transcript(transcript_path, _MAIN_API_ERROR, _NEXT_TURN_RECORDS["queued_prompt"])
        state = await _poll_transcript(client, bridge_dir, state, dedupe)
        bridge.record_hook_event(bridge_dir, _MAIN_STOP_FAILURE)
        await _poll_hooks(
            client,
            bridge_dir,
            forwarder.HookForwardState(event_cursor=0, byte_offset=0),
            dedupe,
            state.current_response_id,
        )
        await _poll_transcript(client, bridge_dir, state, dedupe)

    # The retried error stays in the failed turn instead of starting a new one.
    assert error_posts == [failed_response_id, failed_response_id]
    (failure,) = failures
    assert failure["response_id"] == failed_response_id
    assert failure["failure_detail"] == "Synthetic spend limit reached"


@pytest.mark.asyncio
async def test_api_error_is_not_reused_once_its_turn_ended(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _append_transcript(
        transcript_path,
        {"type": "user", "uuid": "prompt", "message": {"role": "user", "content": "Run checks"}},
        _MAIN_API_ERROR,
    )
    failures: list[dict[str, object]] = []
    dedupe = forwarder._ForwardDedupeState()
    async with httpx.AsyncClient(
        transport=_record_events(failures), base_url="http://test"
    ) as client:
        state = forwarder.TranscriptForwardState(transcript_path, 0)
        hooks = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
        # The second turn fails before Claude writes anything for it.
        for next_turn in ((), (_NEXT_TURN_RECORDS["queued_prompt"],)):
            _append_transcript(transcript_path, *next_turn)
            bridge.record_hook_event(bridge_dir, _MAIN_STOP_FAILURE)
            state = await _poll_transcript(client, bridge_dir, state, dedupe)
            hooks = await _poll_hooks(client, bridge_dir, hooks, dedupe, state.current_response_id)

    first, second = failures
    assert first["failure_detail"] == "Synthetic spend limit reached"
    assert second["failure_detail"] == (
        "Claude Code ended the turn with an API error (authentication_failed)."
    )
    assert second["failure_context"]["detail_source"] == "hook_error_category"


@pytest.mark.asyncio
@pytest.mark.parametrize("telemetry_failure", [False, True])
@pytest.mark.parametrize("previous_attempt_failed", [False, True])
async def test_completed_parent_turn_supersedes_an_offline_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    telemetry_failure: bool,
    previous_attempt_failed: bool,
) -> None:
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions

    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    _append_transcript(
        transcript_path,
        {
            "type": "user",
            "uuid": "failed-prompt",
            "message": {"role": "user", "content": "Run checks"},
        },
        _MAIN_API_ERROR,
    )
    bridge.record_hook_event(bridge_dir, _MAIN_STOP_FAILURE)
    failed_retry_key = f"hook:1:{(bridge_dir / 'hooks.jsonl').stat().st_size}:failed"
    retry_tracker = forwarder._PostRetryTracker(base_delay_s=60, max_delay_s=60)
    items: list[dict[str, object]] = []
    status_posts: list[str] = []
    success_calls: list[None] = []
    successes_before_hooks = 0
    note_forward_success = forwarder._note_forward_success

    def note_success() -> None:
        success_calls.append(None)
        note_forward_success()

    monkeypatch.setattr(forwarder, "_note_forward_success", note_success)

    def handle(request: httpx.Request) -> httpx.Response:
        event = json.loads(request.content)
        if event["type"] == "external_session_status":
            data = event["data"]
            status_posts.append(data["status"])
            if data["status"] == "failed" and previous_attempt_failed:
                return httpx.Response(503)
            assert not retry_tracker.has_retry_state(failed_retry_key)
            assert len(success_calls) == successes_before_hooks
            sessions._publish_status(
                "conv_synthetic",
                data["status"],
                response_id=data.get("response_id"),
                persist_live_status=False,
            )
        elif event["type"] == "external_conversation_item":
            items.append(event["data"])
        return httpx.Response(202, json={})

    if telemetry_failure:

        def fail(*_args: object, **_kwargs: object) -> None:
            raise TypeError("synthetic telemetry failure")

        monkeypatch.setattr(forwarder, "_stop_failure_context", fail)
    caplog.set_level(logging.DEBUG, logger=forwarder.__name__)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://test"
        ) as client:
            dedupe = forwarder._ForwardDedupeState()
            state = await _poll_transcript(
                client, bridge_dir, forwarder.TranscriptForwardState(transcript_path, 0), dedupe
            )
            hooks = forwarder.HookForwardState(0, 0)
            if previous_attempt_failed:
                hooks = await _poll_hooks(
                    client,
                    bridge_dir,
                    hooks,
                    dedupe,
                    state.current_response_id,
                    retry_tracker=retry_tracker,
                )
                assert hooks.event_cursor == 0
                assert retry_tracker.has_retry_state(failed_retry_key)
                assert retry_tracker.retry_delay_s(failed_retry_key) is not None
            _append_transcript(
                transcript_path,
                _NEXT_TURN_RECORDS["queued_prompt"],
                {
                    "type": "assistant",
                    "uuid": "later-success",
                    "message": {"role": "assistant", "content": "Checks passed"},
                },
            )
            for hook in (
                {"hook_event_name": "UserPromptSubmit", "session_id": "native-session"},
                {"hook_event_name": "Stop", "session_id": "native-session"},
            ):
                bridge.record_hook_event(bridge_dir, hook)
            state = await _poll_transcript(client, bridge_dir, state, dedupe)
            successes_before_hooks = len(success_calls)
            hooks = await _poll_hooks(
                client,
                bridge_dir,
                hooks,
                dedupe,
                state.current_response_id,
                retry_tracker=retry_tracker,
            )
        assert sessions._session_status_cache["conv_synthetic"] == "idle"
        assert any("main-error" in str(item["source_id"]) for item in items)
        assert hooks.event_cursor == 3
        assert status_posts == (["failed", "idle"] if previous_attempt_failed else ["idle"])
        assert not retry_tracker.has_retry_state(failed_retry_key)
        if telemetry_failure:
            assert "Claude historical failure telemetry failed: TypeError" in caplog.text
        else:
            observed = next(
                record
                for record in caplog.records
                if getattr(record, "attributes", {}).get("failure_decision") == "superseded"
            )
            assert observed.attributes["native_error_category"] == "authentication_failed"
            assert observed.attributes["native_hook_event"] == "StopFailure"
            assert observed.attributes["suppression_reason"] == "later_completed_parent_turn"
            assert observed.attributes["failure_id"]
            assert "response_id" not in observed.attributes
    finally:
        sessions._session_status_cache.pop("conv_synthetic", None)
        sessions._session_active_response_cache.pop("conv_synthetic", None)
        session_stream.close("conv_synthetic")


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("completed_parent", {1}),
        ("compacted_parent", {1}),
        ("resumed_parent", {1}),
        ("cleared_parent", set()),
        ("forked_parent", set()),
        ("subagent", set()),
        ("subagent_submit", set()),
        ("subagent_stop", set()),
        ("no_submit", set()),
        ("active_parent", set()),
        ("latest_offline_failure", set()),
        ("newer_failed_turn", set()),
        ("failed_after_completion", {1}),
        ("unknown_native_session", set()),
        ("unresolved_parent", set()),
    ],
)
def test_only_a_later_completed_parent_turn_supersedes_a_failure(
    scenario: str, expected: set[int]
) -> None:
    records = [
        bridge.ClaudeHookRecord(1, 10, "StopFailure", claude_session_id="parent"),
        bridge.ClaudeHookRecord(2, 20, "UserPromptSubmit", claude_session_id="parent"),
        bridge.ClaudeHookRecord(3, 30, "Stop", claude_session_id="parent"),
    ]
    if scenario in {"compacted_parent", "resumed_parent", "cleared_parent"}:
        source = {
            "compacted_parent": "compact",
            "resumed_parent": "resume",
            "cleared_parent": "clear",
        }[scenario]
        records.insert(
            1,
            bridge.ClaudeHookRecord(
                1, 15, "SessionStart", source=source, claude_session_id="parent"
            ),
        )
    elif scenario in {"forked_parent", "subagent", "unknown_native_session"}:
        changes = (
            {"claude_session_id": "fork"}
            if scenario == "forked_parent"
            else {"agent_id": "child"}
            if scenario == "subagent"
            else {"claude_session_id": None}
        )
        records[1:] = [replace(record, **changes) for record in records[1:]]
    elif scenario in {"subagent_submit", "subagent_stop"}:
        index = 1 if scenario == "subagent_submit" else 2
        records[index] = replace(records[index], agent_id="child")
    elif scenario == "no_submit":
        records.pop(1)
    elif scenario == "active_parent":
        records.pop()
    elif scenario == "latest_offline_failure":
        records = records[:1]
    elif scenario == "newer_failed_turn":
        records[-1] = bridge.ClaudeHookRecord(3, 30, "StopFailure", claude_session_id="parent")
    elif scenario == "failed_after_completion":
        records.extend(
            [
                bridge.ClaudeHookRecord(4, 40, "UserPromptSubmit", claude_session_id="parent"),
                bridge.ClaudeHookRecord(5, 50, "StopFailure", claude_session_id="parent"),
            ]
        )
    records = [
        replace(record, event_cursor=index, byte_offset=index * 10, recorded_at=1.0)
        for index, record in enumerate(records, start=1)
    ]
    parent_sessions = set() if scenario == "unresolved_parent" else {"parent", "fork"}
    assert forwarder._superseded_failure_cursors(records, parent_sessions) == expected


@pytest.mark.asyncio
async def test_hook_category_and_identity_survive_retry(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "native-session",
            "error": "server_error",
            "error_details": "API Error: 500 Overloaded",
            "last_assistant_message": "I am waiting for a background task.",
        },
    )
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(503 if len(requests) == 1 else 204)

    initial = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        for _ in range(2):
            # Recreate the tracker to simulate retry after a forwarder restart.
            state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_synthetic",
                bridge_dir=bridge_dir,
                state=initial,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id="resp_synthetic",
                diagnostic_health=lambda: {
                    "diagnostic_capture_state": "disabled",
                    "diagnostic_capture_enabled": False,
                },
            )
    assert state.event_cursor == 1
    assert len(requests) == 2
    first, second = [request["data"] for request in requests]
    assert first == second
    assert second["failure_context"]["native_error_category"] == "server_error"
    assert second["failure_context"]["detail_source"] == "hook_error_details"
    assert second["failure_context"]["failure_id"]
    assert second["failure_context"]["native_hook_recorded_at"] > 0
    assert second["failure_context"]["native_session_id"] == "native-session"
    assert second["failure_context"]["diagnostic_capture_enabled"] is False
    assert second["response_id"] == "resp_synthetic"
    assert second["failure_detail"] == "API Error: 500 Overloaded"
    assert "native_api_error_message" not in second["failure_context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["transcript_lookup", "context_assembly"])
@pytest.mark.parametrize("hook_category", [None, "server_error"])
async def test_hook_telemetry_failure_does_not_block_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_phase: str,
    hook_category: str | None,
) -> None:
    bridge_dir = tmp_path / "bridge"
    hook = {"hook_event_name": "StopFailure", "session_id": "native-session"}
    if hook_category is not None:
        hook["error"] = hook_category
    if failure_phase == "context_assembly":
        hook["error_details"] = "Generation failed."
    bridge.record_hook_event(
        bridge_dir,
        hook,
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise TypeError("synthetic-private-telemetry-data")

    monkeypatch.setattr(
        forwarder,
        "_stop_failure_context"
        if failure_phase == "context_assembly"
        else "_transcript_api_error_for_hook",
        fail,
    )
    caplog.set_level(logging.DEBUG, logger=forwarder.__name__)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        for _ in range(2):
            state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_synthetic",
                bridge_dir=bridge_dir,
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id="resp_synthetic",
            )
    assert state.event_cursor == 1
    assert state.byte_offset == (bridge_dir / "hooks.jsonl").stat().st_size
    assert len(requests) == 1
    assert requests[0]["type"] == "external_session_status"
    data = requests[0]["data"]
    context = data.pop("failure_context")
    expected = {"status": "failed", "response_id": "resp_synthetic"}
    if failure_phase == "context_assembly":
        expected["failure_detail"] = "Generation failed."
    elif hook_category is not None:
        expected["failure_detail"] = "Claude Code ended the turn with an API error (server_error)."
    assert data == expected
    assert context["failure_source"] == "claude_hook"
    assert context["failure_context_error_type"] == "TypeError"
    assert context["failure_context_error_phase"] == failure_phase
    assert context["native_harness"] == "claude-native"
    assert context["native_hook_event"] == "StopFailure"
    assert context["native_hook_cursor"] == state.event_cursor
    assert context["native_hook_offset"] == state.byte_offset
    assert context["native_hook_recorded_at"] > 0
    assert context["native_session_id"] == "native-session"
    assert context.get("native_error_category") == hook_category
    assert context["runner_version"] == forwarder.VERSION
    assert context["failure_id"]
    assert "synthetic-private-telemetry-data" not in json.dumps(context)
    assert "Claude hook failure telemetry failed: TypeError" in caplog.text
    assert "synthetic-private-telemetry-data" not in caplog.text


@pytest.mark.parametrize("marker_location", ["entry", "message"])
@pytest.mark.parametrize("block_count", [1, 2], ids=["string", "multiple-text-blocks"])
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("retry_after_rejection", [False, True], ids=["accepted", "retried"])
@pytest.mark.asyncio
async def test_explicit_transcript_api_error_is_observed_without_failing_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    marker_location: str,
    block_count: int,
    batch: bool,
    retry_after_rejection: bool,
) -> None:
    error_text = (
        'API Error: 400 {"error":{"type":"invalid_request_error","code":"invalid_user",'
        '"param":"user","message":"User field is too long"},"request_id":"error-body-id"}'
    )
    entry = {
        "type": "assistant",
        "uuid": "native-record",
        "sessionId": "native-session",
        "requestId": "native-request",
        "request_id": "unqualified-record-request",
        "version": "2.0.0-test",
        "message": {"role": "assistant", "model": "synthetic-model", "content": error_text},
    }
    if block_count == 2:
        entry["message"]["content"] = [
            {"type": "text", "text": error_text},
            {"type": "text", "text": "Provider failed after retrying."},
        ]
    if marker_location == "entry":
        entry["isApiErrorMessage"] = True
    else:
        entry["message"]["isApiErrorMessage"] = True
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(entry) + "\n")
    _, _, items = bridge.read_transcript_items_since(path, 0, agent_name="test-agent")
    assert len(items) == block_count
    posted = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(
            422 if retry_after_rejection and len(posted) == 1 else 202,
            json=[{"item_id": f"item-{index}"} for index in range(block_count)] if batch else {},
            headers={"x-request-id": "omnigent-event-post-id"},
        )

    caplog.set_level(logging.INFO, logger=forwarder.__name__)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:

        async def post() -> None:
            if batch:
                await forwarder._post_external_conversation_item_batch(
                    client,
                    session_id="conv_synthetic",
                    items=[forwarder._PendingSubagentItem(item=item) for item in items],
                )
            else:
                for item in items:
                    await forwarder._post_external_conversation_item(
                        client, session_id="conv_synthetic", item=item
                    )

        if retry_after_rejection:
            with pytest.raises(httpx.HTTPStatusError):
                await post()
            assert any(
                getattr(r, "event_name", None) == "native_failure_observed" for r in caplog.records
            )
        await post()
    observations = block_count
    if retry_after_rejection:
        observations += block_count if batch else 1
    events = [event for payload in posted for event in (payload if batch else [payload])]
    assert [event["type"] for event in events] == ["external_conversation_item"] * observations
    assert events[0]["data"]["item_data"]["content"][0]["text"] == error_text
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "native_failure_observed"
    ]
    assert len(records) == observations
    assert len({record.attributes["failure_id"] for record in records}) == block_count
    assert {record.attributes["native_record_id"] for record in records} == {"native-record"}
    attrs = records[0].attributes
    assert attrs["native_api_error_message"] == "True"
    assert attrs["detail_source"] == "explicit_api_error"
    assert attrs["provider_error_type"] == "invalid_request_error"
    assert attrs["provider_error_code"] == "invalid_user"
    assert attrs["provider_error_param"] == "user"
    assert attrs["native_error_message"] == "User field is too long"
    assert attrs["http_status"] == "400"
    assert attrs["native_request_id"] == "native-request"
    assert attrs["native_error_request_id"] == "error-body-id"
    assert attrs["native_record_id"] == "native-record"
    assert attrs["native_cli_version"] == "2.0.0-test"
    assert attrs["native_model"] == "synthetic-model"
    assert attrs["failure_decision"] == "observed"
    assert "provider_request_id" in attrs["failure_context_missing_fields"]
    assert "gateway_request_id" in attrs["failure_context_missing_fields"]
    assert "omnigent-event-post-id" not in str(attrs)


def test_model_prose_does_not_become_explicit_error_evidence(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "prose",
                "message": {
                    "role": "assistant",
                    "content": "API Error: 400 is an example of the response.",
                },
            }
        )
        + "\n"
    )
    _, _, items = bridge.read_transcript_items_since(path, 0, agent_name="test-agent")
    assert items[0].failure_context is None


def test_api_error_keeps_original_message_before_display_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "api-error",
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Prompt is too long"}],
                },
            }
        )
        + "\n"
    )
    _, _, items = bridge.read_transcript_items_since(path, 0, agent_name="test-agent")
    assert items[0].data["content"][0]["text"].startswith("Context limit reached")
    assert items[0].failure_context["native_error_message"] == "Prompt is too long"


@pytest.mark.parametrize(
    ("entry", "error_text", "expected", "absent"),
    [
        pytest.param(
            {"error": {"code": 400}},
            None,
            {"provider_error_code": "400"},
            (),
            id="numeric-provider-code",
        ),
        pytest.param(
            {"error": {"code": None, "error_code": "stream_lost"}},
            None,
            {"provider_error_code": "stream_lost"},
            (),
            id="null-code-falls-back",
        ),
        pytest.param(
            {"error": {"code": 0, "error_code": "ignored"}},
            None,
            {"provider_error_code": "0"},
            (),
            id="zero-code-is-preserved",
        ),
        pytest.param(
            {
                "requestId": "native-request",
                "request_id": "unqualified-record-request",
                "error": {"code": "stream_lost"},
            },
            None,
            {"native_request_id": "native-request", "provider_error_code": "stream_lost"},
            ("native_error_request_id", "gateway_request_id", "provider_request_id"),
            id="record-request-id-is-not-error-body-evidence",
        ),
        pytest.param(
            {
                "error": None,
                "message": {"error": {"code": "stream_lost", "message": "Lost stream"}},
            },
            None,
            {
                "provider_error_code": "stream_lost",
                "native_error_message": "Lost stream",
                "inference_detail_source": "structured_error",
            },
            ("native_error_category",),
            id="null-entry-error-preserves-message-error",
        ),
        pytest.param(
            {"isApiErrorMessage": True, "error": {"code": "stream_lost"}},
            "Connection lost mid-response",
            {
                "native_error_message": "Connection lost mid-response",
                "provider_error_code": "stream_lost",
            },
            (),
            id="partial-structured-error-retains-explicit-text",
        ),
        pytest.param(
            {"message": {"model": "<synthetic>"}},
            None,
            {},
            ("native_model",),
            id="synthetic-model-is-unavailable",
        ),
        pytest.param(
            {
                "error": {"type": "server_error", "code": "overloaded", "param": None},
                "status_code": 503,
                "gateway_request_id": "gateway-id",
                "provider_request_id": "provider-id",
            },
            None,
            {
                "http_status": 503,
                "provider_error_code": "overloaded",
                "inference_detail_source": "structured_error",
                "gateway_request_id": "gateway-id",
                "provider_request_id": "provider-id",
            },
            ("native_error_category", "provider_error_param"),
            id="structured-error-and-request-ownership",
        ),
    ],
)
def test_native_error_extraction(
    entry: dict[str, object],
    error_text: str | None,
    expected: FailureContext,
    absent: tuple[str, ...],
) -> None:
    context = claude_failure_context(entry, error_text=error_text)
    assert {key: context[key] for key in expected} == expected
    assert not (set(absent) & context.keys())


def test_hook_inference_fields_are_parsed_before_display_truncation(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    error_text = "API Error: 400 " + json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "param": "user",
                "message": "x" * 6000,
            }
        }
    )
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "native-session",
            "error": "invalid_request",
            "error_details": error_text,
        },
    )
    (record,) = bridge.read_hook_events_since_with_position(bridge_dir, 0).records
    assert len(record.failure_message) == 4000
    assert record.failure_context["provider_error_type"] == "invalid_request_error"
    assert record.failure_context["provider_error_param"] == "user"
    assert len(record.failure_context["native_error_message"]) == 1024
