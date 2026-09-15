"""Keep Claude's failure category through hook parsing and status delivery."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge, forwarder


def _write_hook(bridge_dir: Path, payload: dict[str, object]) -> None:
    bridge_dir.mkdir(exist_ok=True)
    (bridge_dir / "hooks.jsonl").write_text(
        json.dumps({"recorded_at": 1.0, "payload": payload}) + "\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("model_not_found", "selected model is unavailable"),
        ("authentication_failed", "authentication failed"),
        ("rate_limit", "rate limit"),
    ],
)
async def test_stop_failure_posts_reason_without_transcript(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str, expected: str
) -> None:
    _write_hook(
        tmp_path,
        {
            "hook_event_name": "StopFailure",
            "error": kind,
            "last_assistant_message": "private transcript text",
        },
    )
    posted: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(202)

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handle)
    ) as client:
        with caplog.at_level(logging.WARNING, logger=forwarder.__name__):
            state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_failed",
                bridge_dir=tmp_path,
                state=forwarder.HookForwardState(event_cursor=0),
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id="resp_failed",
            )
    assert state.event_cursor == 1
    assert len(posted) == 1
    data = posted[0]["data"]
    assert data["status"] == "failed"
    assert data["response_id"] == "resp_failed"
    assert expected in data["output"]
    assert "private transcript text" not in json.dumps(posted)
    records = [r for r in caplog.records if "StopFailure forwarded" in r.getMessage()]
    assert len(records) == 1
    assert records[0].attributes["failure_kind"] == kind
    assert records[0].attributes["response_id"] == "resp_failed"
    assert "private transcript text" not in caplog.text


@pytest.mark.parametrize("raw_error", [None, "unrecognized-private-value", {"message": "private"}])
def test_unknown_hook_error_is_not_copied_to_output_or_diagnostics(
    tmp_path: Path, raw_error: object
) -> None:
    _write_hook(tmp_path, {"hook_event_name": "StopFailure", "error": raw_error})
    record = bridge.read_hook_events_from_offset(tmp_path, 0, start_event_count=0).records[0]
    assert record.failure_kind == ("missing" if raw_error is None else "unrecognized")
    assert record.failure_detail is None


def test_success_hook_does_not_forward_its_error_field(tmp_path: Path) -> None:
    _write_hook(tmp_path, {"hook_event_name": "Stop", "error": "rate_limit"})
    record = bridge.read_hook_events_from_offset(tmp_path, 0, start_event_count=0).records[0]
    assert record.failure_kind is None
    assert record.failure_detail is None
