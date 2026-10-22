"""Diagnostics tests for Claude-native forwarding."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)


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
