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
