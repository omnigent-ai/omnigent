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

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)
from omnigent.runner.transports.ws_tunnel.event_delivery import (
    RunnerEventDispatcher,
    TunnelEventClient,
)

from ._support import _get_recorded_request, _start_recording_server


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


def _record_turn(bridge_dir: Path, transcript_path: Path) -> None:
    for event_name in ("UserPromptSubmit", "Stop"):
        record_hook_event(
            bridge_dir,
            {
                "hook_event_name": event_name,
                "session_id": "claude-session",
                "transcript_path": str(transcript_path),
            },
        )


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
async def test_known_transcript_path_that_never_appears_is_surfaced_after_stop(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook-reported transcript whose file never exists must not settle as a quiet turn.

    Claude Code reports ``transcript_path`` in every hook payload even when it cannot
    create the file (an unwritable ``~/.claude/projects``) and still ends each turn with
    ``Stop``. The forwarder must keep posting the confirmed idle edge, but name the
    unread transcript on that edge, in the session history, and in the runner log.
    The blocker text is injected so the scenario does not depend on filesystem
    privileges; the probes themselves are covered by the focused tests below.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "claude-settings.json").write_text("{}", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    transcript_path = projects / "-home-user-workspace" / "claude-session.jsonl"
    moved_transcript_path = projects / "-home-user-worktree" / "claude-session.jsonl"
    blocker = f"{projects} is not writable (EACCES)"
    monkeypatch.setattr(forwarder, "_transcript_write_blocker", lambda path: blocker)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Four turns; the transcript moves with a worktree and back, so the same
    # path recurs after a different one.
    for turn_path in (transcript_path, transcript_path, moved_transcript_path, transcript_path):
        _record_turn(bridge_dir, turn_path)
    caplog.set_level(logging.INFO, logger=forwarder.__name__)

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_missing_transcript",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    posts: list[dict[str, Any]] = []
    try:
        while len(_idle_edges(posts)) < 4:
            posts.append(await _get_recorded_request(server, timeout_s=10.0))
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
            break
    assert not transcript_path.exists()

    # Every turn still settles as a confirmed completion, but each edge tells the
    # parent the result could not be read instead of "produced no output".
    idle_edges = _idle_edges(posts)
    assert len(idle_edges) == 4
    for edge in idle_edges:
        assert edge["turn_completed"] is True
        assert edge["output"].startswith("Omnigent could not read this turn's output")
        assert blocker in edge["output"]
    assert str(moved_transcript_path) in idle_edges[2]["output"]
    assert str(transcript_path) in idle_edges[3]["output"]

    # One error item per lost turn, each landing before its own idle edge so the
    # parent wake already finds it in the child's history.
    error_indices = [index for index, post in enumerate(posts) if _is_error_item_post(post)]
    idle_indices = [
        index
        for index, post in enumerate(posts)
        if post["body"].get("type") == "external_session_status"
        and post["body"]["data"].get("status") == "idle"
    ]
    assert len(error_indices) == 4
    assert all(
        error_index < idle_index
        for error_index, idle_index in zip(error_indices, idle_indices, strict=True)
    )
    assert all(
        idle_index < next_error_index
        for idle_index, next_error_index in zip(idle_indices, error_indices[1:], strict=False)
    )
    error_items = [posts[index]["body"]["data"] for index in error_indices]
    assert len({item["source_id"] for item in error_items}) == 4
    for item in error_items:
        assert item["item_data"]["source"] == "harness"
        assert item["item_data"]["code"] == "claude_transcript_missing"
        assert item["item_data"]["title"] == "Claude Code's transcript was never written"
        assert blocker in item["item_data"]["cause"]
        assert "writable" in item["item_data"]["remediation"]
    assert str(transcript_path) in error_items[0]["item_data"]["message"]
    assert str(moved_transcript_path) in error_items[2]["item_data"]["message"]

    # The runner log names each path and the directory's errno once, even when a
    # path recurs after the transcript moved elsewhere.
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.levelno for record in warnings] == [logging.WARNING, logging.WARNING]
    assert str(transcript_path) in warnings[0].getMessage()
    assert str(moved_transcript_path) in warnings[1].getMessage()
    assert all(blocker in record.getMessage() for record in warnings)
    assert warnings[0].session_id == "conv_missing_transcript"


@pytest.mark.parametrize(
    ("relative_path", "file_in_the_way", "expected"),
    [
        pytest.param(
            Path("projects") / "missing-slug" / "claude-session.jsonl",
            None,
            "{parent} does not exist (ENOENT); {root}/projects is writable",
            id="missing-parent-under-writable-dir",
        ),
        pytest.param(
            Path("projects") / "claude-session.jsonl",
            None,
            "{parent} is writable but the file was never created",
            id="writable-parent-file-never-created",
        ),
        pytest.param(
            Path("projects") / "slug" / "claude-session.jsonl",
            Path("projects") / "slug",
            "{parent} is not a directory (ENOTDIR)",
            id="regular-file-where-the-parent-should-be",
        ),
    ],
)
def test_transcript_write_blocker_names_the_missing_or_writable_directory(
    tmp_path: Path, relative_path: Path, file_in_the_way: Path | None, expected: str
) -> None:
    """Without a permission problem the diagnostic still says which directory is missing."""
    (tmp_path / "projects").mkdir()
    if file_in_the_way is not None:
        (tmp_path / file_in_the_way).write_text("", encoding="utf-8")
    transcript_path = tmp_path / relative_path
    assert forwarder._transcript_write_blocker(transcript_path) == expected.format(
        parent=transcript_path.parent, root=tmp_path
    )


def test_unwritable_directory_errno_names_a_read_only_mount(tmp_path: Path) -> None:
    """An unwritable directory on a read-only mount is EROFS; otherwise it is EACCES."""
    read_only = forwarder._unwritable_directory_errno(tmp_path, mount_is_read_only=lambda p: True)
    writable_mount = forwarder._unwritable_directory_errno(
        tmp_path, mount_is_read_only=lambda p: False
    )
    assert (read_only, writable_mount) == ("EROFS", "EACCES")
    assert forwarder._unwritable_directory_errno(tmp_path) == "EACCES"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_transcript_write_blocker_names_an_unwritable_directory(tmp_path: Path) -> None:
    """The real permission probe names the first existing ancestor that refuses writes."""
    projects = tmp_path / "projects"
    projects.mkdir()
    projects.chmod(0o555)
    try:
        blocker = forwarder._transcript_write_blocker(projects / "slug" / "claude-session.jsonl")
    finally:
        projects.chmod(0o755)
    assert blocker == f"{projects} is not writable (EACCES)"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_missing_transcript_blocker_treats_an_unsearchable_directory_as_unreadable(
    tmp_path: Path,
) -> None:
    """A transcript under a directory the runner cannot search is reported, not raised."""
    locked = tmp_path / "locked"
    locked.mkdir()
    transcript_path = locked / "slug" / "claude-session.jsonl"
    locked.chmod(0o000)
    try:
        probe = forwarder._missing_transcript_blocker(tmp_path / "bridge", transcript_path)
    finally:
        locked.chmod(0o755)
    assert probe == forwarder._UnreadTranscript(
        transcript_path, f"{transcript_path.parent} is not accessible (EACCES)", exists=False
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_missing_transcript_blocker_reports_an_unreadable_existing_file(tmp_path: Path) -> None:
    """A transcript that exists but cannot be read is classified instead of passing silently."""
    transcript_path = tmp_path / "claude-session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    transcript_path.chmod(0o000)
    try:
        probe = forwarder._missing_transcript_blocker(tmp_path / "bridge", transcript_path)
    finally:
        transcript_path.chmod(0o644)
    assert probe == forwarder._UnreadTranscript(
        transcript_path, f"{transcript_path} is not readable (EACCES)", exists=True
    )
    assert forwarder._missing_transcript_blocker(tmp_path / "bridge", transcript_path) is None


@pytest.mark.asyncio
async def test_failed_missing_transcript_diagnostic_does_not_block_the_idle_edge(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that raises is logged; the turn's confirmed idle edge still posts."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _record_turn(bridge_dir, tmp_path / "claude-session.jsonl")

    def exploding_probe(bridge_dir: Path, hook_transcript_path: Path | None) -> None:
        raise PermissionError("probe denied")

    monkeypatch.setattr(forwarder, "_missing_transcript_blocker", exploding_probe)
    caplog.set_level(logging.INFO, logger=forwarder.__name__)
    requests: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        transport=_recording_transport(requests), base_url="http://test"
    ) as client:
        await _forward_stop_edge(client, bridge_dir, "conv_probe_failed")

    assert requests == [
        {
            "type": "external_session_status",
            "data": {"status": "idle", "background_task_count": 0, "turn_completed": True},
        }
    ]
    failures = [
        record
        for record in caplog.records
        if "missing-transcript diagnostic failed" in record.getMessage()
    ]
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING
    assert failures[0].session_id == "conv_probe_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rejections", "notice_delivered"),
    [pytest.param(1, True, id="transient"), pytest.param(10, False, id="persistent")],
)
async def test_missing_transcript_notice_retries_then_still_posts_the_idle_edge(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    rejections: int,
    notice_delivered: bool,
) -> None:
    """A rejected chat notice is retried under its stable id before the idle edge.

    The retries are bounded, so completion is delayed at most briefly and the
    idle edge posts whether or not the notice landed.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "never-written.jsonl"
    _record_turn(bridge_dir, transcript_path)
    caplog.set_level(logging.WARNING, logger=forwarder.__name__)
    requests: list[dict[str, Any]] = []
    remaining_rejections = rejections

    def _reject_notices(payload: dict[str, Any]) -> httpx.Response:
        nonlocal remaining_rejections
        if payload["type"] == "external_conversation_item" and remaining_rejections > 0:
            remaining_rejections -= 1
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=_recording_transport(requests, _reject_notices), base_url="http://test"
    ) as client:
        await _forward_stop_edge(client, bridge_dir, "conv_retry")

    notices = [
        request["data"] for request in requests if request["type"] == "external_conversation_item"
    ]
    statuses = [
        request["data"] for request in requests if request["type"] == "external_session_status"
    ]
    expected_attempts = min(rejections + 1, forwarder._MISSING_TRANSCRIPT_NOTICE_ATTEMPTS)
    assert len(notices) == expected_attempts
    assert len({notice["source_id"] for notice in notices}) == 1
    assert [status["status"] for status in statuses] == ["idle"]
    assert statuses[0]["turn_completed"] is True
    assert str(transcript_path) in statuses[0]["output"]
    # Every notice attempt precedes the edge; the edge posts whether or not one landed.
    assert requests[-1]["type"] == "external_session_status"
    dropped = [
        record
        for record in caplog.records
        if "Failed to post Claude missing-transcript notice" in record.getMessage()
    ]
    assert len(dropped) == (0 if notice_delivered else 1)


@pytest.mark.asyncio
async def test_missing_transcript_notice_deadline_frees_the_idle_edge_during_a_tunnel_outage(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tunnel that stops acknowledging cannot hold the confirmed idle edge behind the notice.

    Source-keyed items ride the tunnel and wait for its next connection, while
    the status edge uses HTTP. Once the notice deadline passes, the edge still
    posts with its diagnostic output.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "never-written.jsonl"
    _record_turn(bridge_dir, transcript_path)
    monkeypatch.setattr(forwarder, "_MISSING_TRANSCRIPT_NOTICE_DEADLINE_S", 0.3)
    caplog.set_level(logging.WARNING, logger=forwarder.__name__)

    async def _send(_frame: str) -> None:
        return None

    dispatcher = RunnerEventDispatcher()
    dispatcher.ready(_send)
    dispatcher.disconnected()
    requests: list[dict[str, Any]] = []
    started = asyncio.get_running_loop().time()
    async with TunnelEventClient(
        event_dispatcher=dispatcher,
        transport=_recording_transport(requests),
        base_url="http://test",
    ) as client:
        await _forward_stop_edge(client, bridge_dir, "conv_tunnel_outage")
    elapsed = asyncio.get_running_loop().time() - started

    # The notice waited on the tunnel and never reached HTTP; the edge did.
    assert [request["type"] for request in requests] == ["external_session_status"]
    assert requests[0]["data"]["turn_completed"] is True
    assert str(transcript_path) in requests[0]["data"]["output"]
    assert 0.3 <= elapsed < 5.0
    dropped = [
        record
        for record in caplog.records
        if "Failed to post Claude missing-transcript notice" in record.getMessage()
    ]
    assert len(dropped) == 1
    assert "outcome=deadline" in dropped[0].getMessage()


@pytest.mark.asyncio
async def test_unreadable_existing_transcript_gets_its_own_wording(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript classified as unreadable is explained as such, not as never written."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "claude-session.jsonl"
    _record_turn(bridge_dir, transcript_path)
    unread = forwarder._UnreadTranscript(
        transcript_path, f"{transcript_path} is not readable (EACCES)", exists=True
    )
    monkeypatch.setattr(forwarder, "_missing_transcript_blocker", lambda bridge_dir, path: unread)
    caplog.set_level(logging.WARNING, logger=forwarder.__name__)
    requests: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        transport=_recording_transport(requests), base_url="http://test"
    ) as client:
        await _forward_stop_edge(client, bridge_dir, "conv_unreadable")

    [notice] = [r["data"] for r in requests if r["type"] == "external_conversation_item"]
    [status] = [r["data"] for r in requests if r["type"] == "external_session_status"]
    assert notice["item_data"]["title"] == "Claude Code's transcript could not be read"
    assert "cannot read it" in notice["item_data"]["message"]
    assert notice["item_data"]["cause"] == f"{transcript_path} is not readable (EACCES)."
    assert "readable by the session user" in notice["item_data"]["remediation"]
    assert status["turn_completed"] is True
    assert f"{transcript_path} exists but could not be read" in status["output"]
    warned = [r for r in caplog.records if "exists but could not be read" in r.getMessage()]
    assert len(warned) == 1
