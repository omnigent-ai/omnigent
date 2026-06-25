"""Tests for the shared native-forwarder POST delivery classifier."""

from __future__ import annotations

import httpx
import pytest

from omnigent._native_post_delivery import post_may_have_been_delivered


@pytest.mark.parametrize(
    "exc,may_have_been_delivered",
    [
        # Server responded with a status — the events route returns 2xx
        # only after the append + consume publish, so any non-2xx means
        # the item was NOT committed. Safe to retry.
        (
            httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://test"),
                response=httpx.Response(503),
            ),
            False,
        ),
        # Connection never established / pool never acquired — no request
        # bytes were sent, so the item was not delivered. Safe to retry.
        (httpx.ConnectError("refused", request=httpx.Request("POST", "http://test")), False),
        (
            httpx.ConnectTimeout("slow connect", request=httpx.Request("POST", "http://test")),
            False,
        ),
        (httpx.PoolTimeout("no slot", request=httpx.Request("POST", "http://test")), False),
        # Request was sent and no response was seen — the server may have
        # committed it. Ambiguous: a retry could duplicate.
        (httpx.ReadTimeout("no response", request=httpx.Request("POST", "http://test")), True),
        (httpx.WriteError("write failed", request=httpx.Request("POST", "http://test")), True),
        (
            httpx.RemoteProtocolError("peer closed", request=httpx.Request("POST", "http://test")),
            True,
        ),
    ],
)
def test_post_may_have_been_delivered_classification(
    exc: httpx.HTTPError, may_have_been_delivered: bool
) -> None:
    """
    Classify which POST failures may have reached + committed the server.

    A forwarder must not retry a POST that may already be committed,
    because external conversation items are not deduped server-side, so
    a retry would surface as a duplicate bubble in the web UI.
    A wrong classification means either duplicates (ambiguous error
    marked safe-to-retry) or lost messages (provably-undelivered error
    marked ambiguous and dropped).

    :param exc: HTTP exception raised while posting an AP event.
    :param may_have_been_delivered: Whether the request may have been
        committed despite the error.
    """
    assert post_may_have_been_delivered(exc) is may_have_been_delivered


async def test_retry_loop_records_exhausted_connectivity_failure_for_watchdog() -> None:
    """An exhausted-retry connectivity failure is recorded for the idle watchdog.

    Writer half of issue #1119: when every POST attempt raises a connection
    error (e.g. ``No route to host``), the shared retry loop must record the
    failure in ``_native_forwarder_health`` so the harness idle-turn watchdog
    can name the real cause. Fails before the recording call was added (the
    health slot stays empty); passes after.
    """
    from omnigent import _native_forwarder_health as health
    from omnigent._native_post_delivery import post_session_event_with_retry

    class _AlwaysConnectError:
        """Stub client whose every POST fails to connect."""

        async def post(self, url: str, *, json: object) -> httpx.Response:
            """Raise a connect error mimicking an unreachable server."""
            del json
            raise httpx.ConnectError("No route to host", request=httpx.Request("POST", url))

    async def _no_sleep(_: float) -> None:
        """No-op sleep so retries don't add real delay."""

    health.clear()
    try:
        result = await post_session_event_with_retry(
            client=_AlwaysConnectError(),  # type: ignore[arg-type]
            url="/v1/sessions/conv_x/events",
            payload={"type": "external_session_status", "data": {}},
            event_type="external_session_status",
            max_attempts=2,
            retry_status_codes=frozenset(),
            sleep=_no_sleep,
            retry_delay=lambda _attempt: 0.0,
            logger_name="test.native_post_delivery",
        )
        assert result is None
        detail = health.recent_post_failure(60.0)
        assert detail is not None
        assert "external_session_status" in detail
        assert "No route to host" in detail
    finally:
        health.clear()


async def test_retry_loop_success_clears_a_prior_connectivity_failure() -> None:
    """A successful POST clears a previously recorded connectivity failure.

    Misattribution guard for issue #1119: once the server is reachable again,
    the retry loop must empty the failure slot so the idle watchdog can't blame
    a long-resolved outage for a later, unrelated stall.
    """
    from omnigent import _native_forwarder_health as health
    from omnigent._native_post_delivery import post_session_event_with_retry

    class _Ok:
        """Stub client whose POST always succeeds with 200."""

        async def post(self, url: str, *, json: object) -> httpx.Response:
            """Return a 200 response."""
            del json
            return httpx.Response(200, request=httpx.Request("POST", url))

    async def _no_sleep(_: float) -> None:
        """No-op sleep."""

    health.clear()
    try:
        # Simulate an earlier outage still on record.
        health.record_post_failure(
            "external_session_status", httpx.ConnectError("No route to host")
        )
        assert health.recent_post_failure(60.0) is not None
        response = await post_session_event_with_retry(
            client=_Ok(),  # type: ignore[arg-type]
            url="/v1/sessions/conv_x/events",
            payload={"type": "external_session_status", "data": {}},
            event_type="external_session_status",
            max_attempts=2,
            retry_status_codes=frozenset(),
            sleep=_no_sleep,
            retry_delay=lambda _attempt: 0.0,
            logger_name="test.native_post_delivery",
        )
        assert response is not None
        assert response.status_code == 200
        # The successful round-trip must have cleared the stale failure.
        assert health.recent_post_failure(60.0) is None
    finally:
        health.clear()
