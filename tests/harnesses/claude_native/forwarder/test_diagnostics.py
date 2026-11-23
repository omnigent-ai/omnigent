"""Diagnostics tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.bridge as bridge
import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)

from ._support import _start_recording_server


def test_observer_hook_stderr_is_logged_incrementally(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hook process errors reach the session-scoped runner log exactly once."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    stderr_path = bridge_dir / forwarder.OBSERVER_HOOK_STDERR_FILE
    stderr_path.write_text("ModuleNotFoundError: No module named 'omnigent'\n", encoding="utf-8")
    caplog.set_level(logging.ERROR, logger=forwarder.__name__)

    offset = forwarder._log_new_observer_hook_stderr(
        bridge_dir=bridge_dir,
        session_id="conv_abc",
        byte_offset=0,
    )
    assert offset == stderr_path.stat().st_size
    assert "No module named 'omnigent'" in caplog.text
    assert caplog.records[-1].session_id == "conv_abc"

    with stderr_path.open("a", encoding="utf-8") as handle:
        handle.write("PermissionError: bridge directory is not writable\n")
    offset = forwarder._log_new_observer_hook_stderr(
        bridge_dir=bridge_dir,
        session_id="conv_abc",
        byte_offset=offset,
    )
    assert offset == stderr_path.stat().st_size
    assert caplog.text.count("No module named 'omnigent'") == 1
    assert caplog.text.count("bridge directory is not writable") == 1

    record_count = len(caplog.records)
    assert (
        forwarder._log_new_observer_hook_stderr(
            bridge_dir=bridge_dir,
            session_id="conv_abc",
            byte_offset=offset,
        )
        == offset
    )
    assert len(caplog.records) == record_count


def test_missing_transcript_warns_then_escalates_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow observer warns; only a stuck one becomes a session-scoped error."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit"})
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    diagnostics = forwarder._TranscriptDiscoveryDiagnostics(started_at=100.0)
    warning_at = diagnostics.started_at + forwarder._TRANSCRIPT_DISCOVERY_WARNING_S
    error_at = diagnostics.started_at + forwarder._TRANSCRIPT_DISCOVERY_ERROR_S
    caplog.set_level(logging.INFO, logger=forwarder.__name__)

    def observe(now: float, transcript_path: Path | None = None) -> None:
        forwarder._observe_transcript_discovery(
            bridge_dir=bridge_dir,
            session_id="conv_abc",
            transcript_path=transcript_path,
            diagnostics=diagnostics,
            now=now,
        )

    def matching(needle: str) -> list[logging.LogRecord]:
        return [record for record in caplog.records if needle in record.getMessage()]

    observe(warning_at - 0.1)
    assert not matching("still waiting")
    assert not matching("has not started")

    # A slow start warns once and stays a warning, however long it is polled.
    for now in (warning_at, warning_at + 30.0, error_at - 0.1):
        observe(now)
    warnings = matching("still waiting")
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "last_hook=UserPromptSubmit" in warnings[0].getMessage()
    assert "observer_stderr_bytes=missing" in warnings[0].getMessage()
    assert "hook_settings=present" in warnings[0].getMessage()
    assert not matching("has not started")

    # Past the escalation deadline it is stuck, not slow: one error, then quiet.
    for now in (error_at, error_at + 60.0):
        observe(now)
    failures = matching("has not started")
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert failures[0].session_id == "conv_abc"
    assert "last_hook=UserPromptSubmit" in failures[0].getMessage()

    transcript_path = tmp_path / "claude-session.jsonl"
    for now in (error_at + 61.0, error_at + 62.0):
        observe(now, transcript_path)
    assert len(matching("path discovered")) == 1


def test_missing_transcript_discovered_after_warning_never_errors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hook that reports late is a warning the whole way, never an error."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    diagnostics = forwarder._TranscriptDiscoveryDiagnostics(started_at=0.0)
    caplog.set_level(logging.INFO, logger=forwarder.__name__)

    for now in (forwarder._TRANSCRIPT_DISCOVERY_WARNING_S, 100.0):
        forwarder._observe_transcript_discovery(
            bridge_dir=bridge_dir,
            session_id="conv_slow",
            transcript_path=None,
            diagnostics=diagnostics,
            now=now,
        )
    forwarder._observe_transcript_discovery(
        bridge_dir=bridge_dir,
        session_id="conv_slow",
        transcript_path=tmp_path / "claude-session.jsonl",
        diagnostics=diagnostics,
        now=106.0,
    )

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len([r for r in caplog.records if "path discovered" in r.getMessage()]) == 1


def _observe_at_error_deadline(bridge_dir: Path) -> None:
    diagnostics = forwarder._TranscriptDiscoveryDiagnostics(started_at=0.0)
    forwarder._observe_transcript_discovery(
        bridge_dir=bridge_dir,
        session_id="conv_idle",
        transcript_path=None,
        diagnostics=diagnostics,
        now=forwarder._TRANSCRIPT_DISCOVERY_ERROR_S + 1.0,
    )


def test_idle_pane_at_error_deadline_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No hooks file, settings present, no stderr: an unused pane, not a failure."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    caplog.set_level(logging.INFO, logger=forwarder.__name__)

    _observe_at_error_deadline(bridge_dir)

    [record] = [r for r in caplog.records if "has not started" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.discovery_verdict == "idle"


def test_observer_stderr_at_error_deadline_logs_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    (bridge_dir / forwarder.OBSERVER_HOOK_STDERR_FILE).write_text("boom", encoding="utf-8")
    caplog.set_level(logging.INFO, logger=forwarder.__name__)

    _observe_at_error_deadline(bridge_dir)

    [record] = [r for r in caplog.records if "has not started" in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert record.discovery_verdict == "hook_failure"


def test_hooks_file_without_path_at_error_deadline_logs_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit"})
    caplog.set_level(logging.INFO, logger=forwarder.__name__)

    _observe_at_error_deadline(bridge_dir)

    [record] = [r for r in caplog.records if "has not started" in r.getMessage()]
    assert record.levelno == logging.ERROR


@pytest.mark.parametrize("http_status", [None, 503])
async def test_degraded_sync_log_belongs_to_the_destination_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    http_status: int | None,
) -> None:
    from omnigent.debug_logging import current_session_id_scope, record_to_row

    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "parent-session")
    monkeypatch.setattr(forwarder, "_forward_health", forwarder._ForwardHealth())
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "source-1",
                "message": {
                    "id": "message-1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Synthetic reply"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        current_response_id=None,
        seen_source_ids=(),
    )
    tracker = forwarder._PostRetryTracker(base_delay_s=0)
    dedupe = forwarder._ForwardDedupeState()

    def reject(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/child-session/events"
        if http_status is None:
            raise httpx.ConnectError("private transport detail", request=request)
        return httpx.Response(http_status, json={"error": "private response detail"})

    with current_session_id_scope("unrelated-request-session"):
        async with httpx.AsyncClient(
            base_url="http://example.test", transport=httpx.MockTransport(reject)
        ) as client:
            for _ in range(forwarder._FORWARD_DEGRADED_THRESHOLD):
                state = await forwarder._forward_available_items(
                    client=client,
                    session_id="child-session",
                    bridge_dir=tmp_path,
                    agent_name="test-agent",
                    state=state,
                    retry_tracker=tracker,
                    dedupe=dedupe,
                )
        records = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "claude_forward_sync_degraded"
        ]
        assert len(records) == 1
        row = record_to_row(records[0], source="runner")

    assert row["session_id"] == "child-session"
    assert row["attributes"]["exception_type"] == (
        "ConnectError" if http_status is None else "HTTPStatusError"
    )
    assert row["attributes"].get("http_status") == (
        str(http_status) if http_status is not None else None
    )
    assert "private" not in json.dumps(row["attributes"])


@pytest.mark.parametrize("http_status", [None, 403, 503])
def test_forward_failures_escalate_to_degraded_once(
    http_status: int | None, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Sustained forward failures flip the degraded latch exactly once (#1120).

    Network drops previously surfaced only as scattered per-item warnings;
    the latch turns a real outage into a single loud signal and does not
    re-fire per dropped item.
    """
    forwarder._reset_forward_health()
    tracker = forwarder._PostRetryTracker()
    request = httpx.Request("POST", "https://example.test/events?secret=private")
    exc = (
        httpx.ConnectError("private connection detail", request=request)
        if http_status is None
        else httpx.HTTPStatusError(
            "private rejection detail",
            request=request,
            response=httpx.Response(http_status, request=request, text="private body"),
        )
    )

    for _ in range(forwarder._FORWARD_DEGRADED_THRESHOLD - 1):
        tracker.record_failure("item:source-1", exc, session_id="conv_health")
    # Below threshold: not yet degraded.
    assert forwarder._forward_health.degraded_logged is False

    tracker.record_failure("item:source-1", exc, session_id="conv_health")  # crosses threshold
    assert forwarder._forward_health.degraded_logged is True
    assert forwarder._forward_health.consecutive_failures == forwarder._FORWARD_DEGRADED_THRESHOLD

    # The latch holds — further failures keep counting but don't re-escalate.
    tracker.record_failure("item:source-1", exc, session_id="conv_health")
    assert forwarder._forward_health.degraded_logged is True
    assert (
        forwarder._forward_health.consecutive_failures == forwarder._FORWARD_DEGRADED_THRESHOLD + 1
    )
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_forward_sync_degraded"
    ]
    assert len(records) == 1
    assert records[0].session_id == "conv_health"
    assert records[0].attributes == {
        "exception_type": type(exc).__name__,
        "http_status": http_status,
    }
    assert "private" not in records[0].getMessage()
    assert records[0].exc_info is None


def test_forward_success_resets_degraded_state() -> None:
    """
    A successful forward clears the failure count and degraded latch.

    Recovery must re-arm the indicator so a later outage escalates again.
    """
    forwarder._reset_forward_health()
    for _ in range(forwarder._FORWARD_DEGRADED_THRESHOLD):
        forwarder._note_forward_failure(
            "status:idle", httpx.ConnectError("unreachable"), session_id="conv_health"
        )
    assert forwarder._forward_health.degraded_logged is True

    forwarder._note_forward_success()

    assert forwarder._forward_health.consecutive_failures == 0
    assert forwarder._forward_health.degraded_logged is False


def test_retry_tracker_transient_failures_escalate_degraded() -> None:
    """
    Transient failures escalate via the retry tracker boundary (#1120).

    The claude forwarder retries transient errors (connect timeouts, 503s)
    forever, so they never reach the permanent-drop ``exhausted`` path. This
    proves the degraded indicator still fires for that case — the exact
    503/connect-timeout outage #1120 is about — because every
    ``record_failure`` counts, not just exhausted give-ups. A later
    ``clear`` (a post that got through) re-arms the indicator.
    """
    forwarder._reset_forward_health()
    tracker = forwarder._PostRetryTracker()
    transient = httpx.ConnectError("connect timeout")

    for _ in range(forwarder._FORWARD_DEGRADED_THRESHOLD):
        decision = tracker.record_failure("item:source-1", transient, session_id="conv_health")
        # Transient failures are retried, never dropped.
        assert decision.exhausted is False
        assert decision.permanent is False

    assert forwarder._forward_health.degraded_logged is True

    tracker.clear("item:source-1")
    assert forwarder._forward_health.consecutive_failures == 0
    assert forwarder._forward_health.degraded_logged is False


def _idle_edges(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        post["body"]["data"]
        for post in posts
        if post["body"].get("type") == "external_session_status"
        and (post["body"].get("data") or {}).get("status") == "idle"
    ]


def _is_error_item_post(post: dict[str, Any]) -> bool:
    return (
        post["body"].get("type") == "external_conversation_item"
        and post["body"]["data"].get("item_type") == "error"
    )


def _record_turn(
    bridge_dir: Path, transcript_path: Path, *, last_assistant_message: str | None = None
) -> None:
    for event_name in ("UserPromptSubmit", "Stop"):
        payload: dict[str, Any] = {
            "hook_event_name": event_name,
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        }
        if event_name == "Stop" and last_assistant_message is not None:
            payload["last_assistant_message"] = last_assistant_message
        record_hook_event(bridge_dir, payload)


def _recording_transport(
    requests: list[dict[str, Any]],
    respond: Callable[[dict[str, Any]], httpx.Response] | None = None,
) -> httpx.MockTransport:
    """Record every JSON request body; answer 202 unless ``respond`` decides otherwise."""

    def _handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        requests.append(payload)
        return respond(payload) if respond is not None else httpx.Response(202, json={})

    return httpx.MockTransport(_handle)


async def _forward_stop_edge(client: httpx.AsyncClient, bridge_dir: Path, session_id: str) -> None:
    """Run one hook-phase pass from the first record, as the forwarder loop would."""
    await forwarder._forward_available_status_events(
        client=client,
        session_id=session_id,
        bridge_dir=bridge_dir,
        state=forwarder.HookForwardState(event_cursor=0, byte_offset=0),
        retry_tracker=forwarder._PostRetryTracker(),
        dedupe=forwarder._ForwardDedupeState(),
        task_subjects={},
        task_statuses={},
        task_order=[],
    )


@pytest.mark.asyncio
async def test_readable_transcript_with_a_partial_trailing_line_is_not_flagged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transcript Claude is still writing exists, so the turn is not reported as lost."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "claude-session.jsonl"
    transcript_path.write_text('{"type": "assistant", "message": {"conte', encoding="utf-8")
    _record_turn(bridge_dir, transcript_path)
    caplog.set_level(logging.WARNING, logger=forwarder.__name__)
    requests: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        transport=_recording_transport(requests), base_url="http://test"
    ) as client:
        await _forward_stop_edge(client, bridge_dir, "conv_partial")

    assert not [r for r in requests if r["type"] == "external_conversation_item"]
    [status] = [r["data"] for r in requests if r["type"] == "external_session_status"]
    assert "output" not in status
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_readable_transcript_keeps_an_empty_stop_reply_as_authoritative(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A readable transcript with an empty reply settles with an explicit empty output."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "claude-session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _record_turn(bridge_dir, transcript_path, last_assistant_message="")
    requests: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        transport=_recording_transport(requests), base_url="http://test"
    ) as client:
        await _forward_stop_edge(client, bridge_dir, "conv_empty_reply")

    assert not [r for r in requests if r["type"] == "external_conversation_item"]
    [status] = [r["data"] for r in requests if r["type"] == "external_session_status"]
    assert status["output"] == ""


def _priced_assistant_line(uuid: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "message": {
                "id": f"msg-{uuid}",
                "role": "assistant",
                "model": "claude-sonnet-4-5-20250929",
                "content": [{"type": "text", "text": "priced reply"}],
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            },
        }
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_denied_transcript_read_is_not_cached_as_zero_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read refused during cost pricing is not cached as an empty transcript.

    The size-keyed cache is the only memory of a priced transcript, so a refused read
    cached as ``None`` would stay wrong after permission is restored, even with the
    file size unchanged.
    """
    # The test environment has no provider pricing table, so price the model explicitly.
    monkeypatch.setattr(bridge, "_transcript_model_pricing", lambda *a, **k: object())
    monkeypatch.setattr("omnigent.llms.context_window.compute_llm_cost", lambda *a, **k: 0.25)
    transcript_path = tmp_path / "parent.jsonl"
    transcript_path.write_text(_priced_assistant_line("a1") + "\n", encoding="utf-8")
    expected = forwarder.compute_transcript_cumulative_cost(
        transcript_path, include_sidechains=False
    )
    assert expected is not None
    cache: dict[Path, Any] = {}

    transcript_path.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            forwarder._transcript_cost_size_cached(
                transcript_path, include_sidechains=False, cache=cache
            )
    finally:
        transcript_path.chmod(0o644)
    assert cache == {}

    assert (
        forwarder._transcript_cost_size_cached(
            transcript_path, include_sidechains=False, cache=cache
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
async def test_relocated_unreadable_transcript_keeps_the_saved_cursor(tmp_path: Path) -> None:
    """A hook that relocates the transcript to an unreadable file keeps the saved checkpoint.

    The denied-then-recovered sequence: the forwarder has forwarded the original
    transcript; the hook then reports a same-named copy that cannot be read. The
    saved cursor must survive that poll unchanged, and after permission is restored
    the copy must resume from the cursor without re-forwarding earlier items.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    content = _priced_assistant_line("a1") + "\n" + _priced_assistant_line("a2") + "\n"
    old_path = tmp_path / "projects" / "old" / "claude-session.jsonl"
    new_path = tmp_path / "projects" / "worktree" / "claude-session.jsonl"
    old_path.parent.mkdir(parents=True)
    new_path.parent.mkdir(parents=True)
    old_path.write_text(content, encoding="utf-8")
    new_path.write_text(content, encoding="utf-8")
    _record_turn(bridge_dir, old_path)

    server, thread, base_url = _start_recording_server()

    async def run_loop(seconds: float) -> list[dict[str, Any]]:
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_relocated",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        await asyncio.sleep(seconds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        drained: list[dict[str, Any]] = []
        while True:
            try:
                drained.append(server.requests.get_nowait())
            except queue.Empty:
                return drained

    def transcript_items(posts: list[dict[str, Any]]) -> list[str]:
        return [
            post["body"]["data"]["source_id"]
            for post in posts
            if post["body"].get("type") == "external_conversation_item"
            and post["body"]["data"].get("item_type") != "error"
        ]

    try:
        first = transcript_items(await run_loop(1.5))
        assert len(first) >= 2
        state_path = bridge_dir / forwarder._FORWARDER_STATE_FILE
        saved_cursor = state_path.read_bytes()

        new_path.chmod(0o000)
        _record_turn(bridge_dir, new_path)
        denied = transcript_items(await run_loop(1.5))
        assert denied == []
        # The Stop may settle the turn, so only the checkpoint itself must hold still.
        checkpoint = (
            "transcript_path",
            "line_cursor",
            "byte_offset",
            "cursor_fingerprint",
            "seen_source_ids",
        )
        before, after = json.loads(saved_cursor), json.loads(state_path.read_bytes())
        assert {key: after.get(key) for key in checkpoint} == {
            key: before.get(key) for key in checkpoint
        }

        new_path.chmod(0o644)
        recovered = transcript_items(await run_loop(1.5))
        assert recovered == []
    finally:
        new_path.chmod(0o644)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


async def _run_forwarder_until_idle(bridge_dir: Path, *, idle_count: int) -> list[dict[str, Any]]:
    """Run the production forwarder loop until ``idle_count`` confirmed idle edges arrive."""
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_table",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    posts: list[dict[str, Any]] = []

    def drain() -> None:
        while True:
            try:
                posts.append(server.requests.get_nowait())
            except queue.Empty:
                return

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while len(_idle_edges(posts)) < idle_count and loop.time() < deadline:
            drain()
            await asyncio.sleep(0.02)
        # Let any wrongly fired notice land before judging silence.
        await asyncio.sleep(0.3)
        drain()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
    drain()
    return posts


def _write_state_path(bridge_dir: Path, transcript_path: Path) -> None:
    """Advertise a transcript path the way a later hook would, without a Stop."""
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("file", "file"),
        ("absent", "not_yet_present"),
        ("missing-directory", "not_yet_present"),
        ("parent-is-a-file", "unusable"),
        ("directory", "unusable"),
        ("fifo", "unusable"),
    ],
)
def test_transcript_shape_separates_readable_absent_and_unusable(
    tmp_path: Path, shape: str, expected: str
) -> None:
    """The one classifier that decides between reading, waiting, and reporting."""
    parent = tmp_path / "slug"
    parent.mkdir()
    path = parent / "claude-session.jsonl"
    if shape == "missing-directory":
        path = tmp_path / "missing" / "slug" / "claude-session.jsonl"
    elif shape == "file":
        path.write_text("", encoding="utf-8")
    elif shape == "parent-is-a-file":
        (tmp_path / "blocker").write_text("", encoding="utf-8")
        path = tmp_path / "blocker" / "claude-session.jsonl"
    elif shape == "directory":
        path.mkdir()
    elif shape == "fifo":
        os.mkfifo(path)
    assert forwarder._transcript_shape(path) == expected


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_transcript_shape_treats_unsearchable_ancestor_and_unreadable_file_as_unusable(
    tmp_path: Path,
) -> None:
    locked = tmp_path / "locked"
    (locked / "slug").mkdir(parents=True)
    hidden = locked / "slug" / "claude-session.jsonl"
    hidden.write_text("", encoding="utf-8")
    unreadable = tmp_path / "unreadable.jsonl"
    unreadable.write_text("", encoding="utf-8")
    locked.chmod(0o000)
    unreadable.chmod(0o000)
    try:
        assert forwarder._transcript_shape(hidden) == "unusable"
        assert forwarder._transcript_shape(unreadable) == "unusable"
    finally:
        locked.chmod(0o755)
        unreadable.chmod(0o644)


def test_clear_after_directory_removal_delivers_the_first_new_turn(tmp_path: Path) -> None:
    """A /clear whose transcript directory is missing still forwards the first turn once it lands.

    The clear seeds the new conversation's zero-offset checkpoint even though the
    directory does not exist yet; the reattached forwarder must not skip the turn
    Claude writes after the directory reappears.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    slug = tmp_path / "projects" / "slug"
    transcript_path = slug / "claude-session.jsonl"
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "clear",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    async def _writer() -> None:
        await asyncio.sleep(0.3)
        slug.mkdir(parents=True)
        lines = [
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "first"}},
            json.loads(_priced_assistant_line("a1")),
        ]
        transcript_path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        _record_turn(bridge_dir, transcript_path)

    async def _run() -> list[dict[str, Any]]:
        server, thread, base_url = _start_recording_server()
        task = asyncio.create_task(
            forwarder.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_clear_reattach",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=True,
                poll_interval_s=0.01,
            )
        )
        writer = asyncio.create_task(_writer())
        posts: list[dict[str, Any]] = []
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10.0
            while not _idle_edges(posts) and loop.time() < deadline:
                try:
                    posts.append(server.requests.get_nowait())
                except queue.Empty:
                    await asyncio.sleep(0.02)
            await writer
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)
        while True:
            try:
                posts.append(server.requests.get_nowait())
            except queue.Empty:
                return posts

    posts = asyncio.run(_run())

    assert len(_idle_edges(posts)) == 1
    assert "reply" in json.dumps(posts) or "priced reply" in json.dumps(posts)


@pytest.mark.parametrize(
    "shape",
    [
        "parent-is-a-regular-file",
        "directory-at-the-path",
        pytest.param(
            "unreadable-existing-file",
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files"),
        ),
    ],
)
def test_unusable_transcript_reaches_the_stop_edge_without_crash_or_chat_item(
    tmp_path: Path, shape: str, caplog: pytest.LogCaptureFixture
) -> None:
    """An unusable transcript path must not restart the forwarder loop or stall its Stop.

    The poll forwards hook records only, so the turn settles. Nothing is posted to the
    chat and the loop logs no failure.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    if shape == "parent-is-a-regular-file":
        (projects / "slug").write_text("", encoding="utf-8")
        transcript = projects / "slug" / "claude-session.jsonl"
    elif shape == "directory-at-the-path":
        transcript = projects / "claude-session.jsonl"
        transcript.mkdir()
    else:
        transcript = projects / "claude-session.jsonl"
        transcript.write_text("", encoding="utf-8")
        transcript.chmod(0o000)
    _record_turn(bridge_dir, transcript)
    caplog.set_level(logging.WARNING, logger=forwarder.__name__)
    try:
        posts = asyncio.run(_run_forwarder_until_idle(bridge_dir, idle_count=1))
    finally:
        if shape == "unreadable-existing-file":
            transcript.chmod(0o644)

    [edge] = _idle_edges(posts)
    assert edge["turn_completed"] is True
    assert "output" not in edge
    assert [post for post in posts if _is_error_item_post(post)] == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
async def test_unusable_transcript_keeps_the_saved_checkpoint(tmp_path: Path) -> None:
    """A checkpoint read from A survives a poll whose transcript path became unusable."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    slug = tmp_path / "projects" / "slug"
    slug.mkdir(parents=True)
    transcript = slug / "claude-session.jsonl"
    transcript.write_text(
        _priced_assistant_line("a1") + "\n" + _priced_assistant_line("a2") + "\n",
        encoding="utf-8",
    )
    _write_state_path(bridge_dir, transcript)
    await _run_forwarder_until_idle(bridge_dir, idle_count=0)
    state_file = bridge_dir / forwarder._FORWARDER_STATE_FILE
    before = json.loads(state_file.read_text())
    assert before["byte_offset"] > 0

    transcript.unlink()
    transcript.mkdir()
    _record_turn(bridge_dir, transcript)
    posts = await _run_forwarder_until_idle(bridge_dir, idle_count=1)

    assert len(_idle_edges(posts)) == 1
    after = json.loads(state_file.read_text())
    keys = (
        "transcript_path",
        "line_cursor",
        "byte_offset",
        "cursor_fingerprint",
        "seen_source_ids",
    )
    assert {k: after.get(k) for k in keys} == {k: before.get(k) for k in keys}
