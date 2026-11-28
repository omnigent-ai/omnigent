"""Health tests for Codex forwarder."""

from __future__ import annotations

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RaisingPostClient,
    _RecordingClient,
)


@pytest.fixture(autouse=True)
def _isolated_forward_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fwd, "_forward_health", fwd._ForwardHealth())


class _StatusClient:
    """httpx client stub whose ``post`` returns a fixed status code."""

    def __init__(self, status_code: int) -> None:
        """:param status_code: Status to return from every post, e.g. ``400``."""
        self.status_code = status_code
        self.posts = 0

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        """Return the configured status; never raises."""
        del json
        self.posts += 1
        return httpx.Response(self.status_code, request=httpx.Request("POST", url))


def test_forward_failures_escalate_to_degraded_once() -> None:
    """
    Sustained forward failures flip the degraded latch exactly once (#1120).

    Network drops previously surfaced only as scattered per-item warnings;
    the latch turns a real outage into a single loud signal and does not
    re-fire per dropped item.
    """
    result = fwd._PostResult(response=None, transport_error="ConnectError")

    for _ in range(fwd._FORWARD_DEGRADED_THRESHOLD - 1):
        fwd._note_forward_failure("external_output_text_delta", result, "conv_x")
    # Below threshold: not yet degraded.
    assert fwd._forward_health.degraded_logged is False

    fwd._note_forward_failure("external_output_text_delta", result, "conv_x")  # crosses threshold
    assert fwd._forward_health.degraded_logged is True
    assert fwd._forward_health.consecutive_failures == fwd._FORWARD_DEGRADED_THRESHOLD

    # The latch holds — further failures keep counting but don't re-escalate.
    fwd._note_forward_failure("external_output_text_delta", result, "conv_x")
    assert fwd._forward_health.degraded_logged is True
    assert fwd._forward_health.consecutive_failures == fwd._FORWARD_DEGRADED_THRESHOLD + 1


def test_forward_success_resets_degraded_state() -> None:
    """
    A successful forward clears the failure count and degraded latch.

    Recovery must re-arm the indicator so a later outage escalates again.
    """
    result = fwd._PostResult(response=None, transport_error="ConnectError")
    for _ in range(fwd._FORWARD_DEGRADED_THRESHOLD):
        fwd._note_forward_failure("external_session_usage", result, "conv_x")
    assert fwd._forward_health.degraded_logged is True

    fwd._note_forward_success()

    assert fwd._forward_health.consecutive_failures == 0
    assert fwd._forward_health.degraded_logged is False


@pytest.mark.asyncio
async def test_post_session_event_tracks_success_and_failure() -> None:
    """
    _post_session_event classifies each outcome into forward health (#1120).

    A 2xx clears the failure run; a permanent 4xx counts as a failure so a
    sustained outage can escalate.
    """
    # A permanent 4xx is a failure.
    await fwd._post_session_event(
        _StatusClient(400), "conv_x", event_type="external_session_status", data={"status": "idle"}
    )
    assert fwd._forward_health.consecutive_failures == 1

    # A 2xx resets the run.
    await fwd._post_session_event(
        _RecordingClient(), "conv_x", event_type="external_session_status", data={"status": "idle"}
    )
    assert fwd._forward_health.consecutive_failures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "connect", "ambiguous"])
async def test_degraded_log_records_post_failure_classification(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A degraded period records its latest delivery outcome without copying the payload."""
    request = httpx.Request("POST", "https://example.test/events?secret=private")
    client = (
        _StatusClient(403)
        if failure == "http"
        else _RaisingPostClient(
            httpx.ConnectError("private connection detail", request=request)
            if failure == "connect"
            else httpx.ReadTimeout("private timeout detail", request=request)
        )
    )
    for _ in range(fwd._FORWARD_DEGRADED_THRESHOLD + 1):
        await fwd._post_session_event(
            client,
            "conv_failed_post",
            event_type="external_conversation_item",
            data={"body": "private transcript"},
            max_attempts=1,
        )
    records = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "codex_forward_sync_degraded"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.session_id == "conv_failed_post"
    assert record.attributes == {
        "http_status": 403 if failure == "http" else None,
        "transport_error": None
        if failure == "http"
        else "ConnectError"
        if failure == "connect"
        else "ReadTimeout",
        "delivered_ambiguous": failure == "ambiguous",
        "rejection_reason": None,
    }
    assert "private" not in record.getMessage()
    assert record.exc_info is None
