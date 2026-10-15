"""Elicitation tests for Codex forwarder."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd


class _FlakyElicitationClient:
    """
    Elicitation client stub: configurable failures, then HTTP 200.

    Real stub (not MagicMock) so unexpected extra calls surface in
    :attr:`posts`. Each call records ``(url, json)``. The first
    ``transport_failures`` calls raise ``httpx.ReadError`` (a severed
    long-poll) and the next ``gateway_failures`` calls return HTTP 502
    (a proxy gateway error); every later call returns 200 with a
    JSON-RPC result body.

    :param transport_failures: Calls to fail with ``httpx.ReadError``
        before succeeding, e.g. ``1``.
    :param gateway_failures: Calls to answer with HTTP 502 after the
        transport failures, e.g. ``0``.
    """

    def __init__(self, transport_failures: int = 0, gateway_failures: int = 0) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._transport_failures = transport_failures
        self._gateway_failures = gateway_failures

    async def post(self, url: str, *, json: dict, timeout: httpx.Timeout) -> httpx.Response:
        """
        Record the call and fail/succeed per the configured schedule.

        :param url: Request URL, e.g.
            ``"/v1/sessions/conv_x/hooks/codex-elicitation-request"``.
        :param json: Codex JSON-RPC request envelope.
        :param timeout: Per-attempt budget (ignored by the stub).
        :returns: HTTP 502 during the gateway-failure window, else 200
            with a JSON-RPC result body.
        :raises httpx.ReadError: During the transport-failure window.
        """
        self.posts.append((url, deepcopy(json)))
        attempt = len(self.posts)
        if attempt <= self._transport_failures:
            raise httpx.ReadError(
                "proxy severed the long-poll",
                request=httpx.Request("POST", url),
            )
        if attempt <= self._transport_failures + self._gateway_failures:
            return httpx.Response(502, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"action": "accept", "content": {}, "_meta": None},
            request=httpx.Request("POST", url),
        )


async def _instant_retry_sleep(_seconds: float) -> None:
    """
    Drop-in for ``_elicitation_retry_sleep`` that returns at once.

    :param _seconds: Ignored backoff duration.
    :returns: None.
    """
    return


_ELICITATION_EVENT: dict = {
    "id": 7,
    "method": "mcpServer/elicitation/request",
    "params": {"mode": "form", "message": "Pick a date"},
}


@pytest.mark.asyncio
async def test_elicitation_post_reposts_after_transport_cut_with_same_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A severed elicitation long-poll is re-POSTed with the identical envelope.

    This is the invisible-stuck bug for codex sub-agents: one transport
    error used to abandon the prompt to the native-TUI path nobody is
    watching. The envelope must be byte-identical on the retry — the
    server derives the deterministic elicitation id from (session,
    method, rpc id), so an identical re-POST re-parks the SAME prompt
    and keeps the approval card alive.
    """
    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _FlakyElicitationClient(transport_failures=1)

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]  # stub implements the one used method
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is not None
    assert response.status_code == 200
    # 2 = one severed attempt + one successful retry. 1 means the
    # transport error abandoned the prompt (the production bug).
    assert len(client.posts) == 2, f"expected 2 attempts, got {len(client.posts)}"
    # Identical (url, envelope) on the retry is the re-park contract.
    assert client.posts[0] == client.posts[1]
    assert client.posts[0][0] == "/v1/sessions/conv_x/hooks/codex-elicitation-request"


@pytest.mark.asyncio
async def test_elicitation_post_resets_backoff_after_a_gateway_severed_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A gateway-severed HELD poll resets the retry backoff; fast failures grow it.

    The front door caps a request at 300s and answers with 504, so a parked
    approval is severed every five minutes. A backoff that kept doubling
    pushed the re-POST past the server's re-park grace, which cleared the
    approval card to "Resolved elsewhere" between polls. Growth belongs to
    fast failures (a sick or unreachable server), not to a poll the gateway
    held for its full budget.
    """
    loop = asyncio.get_running_loop()
    clock = {"t": 0.0}
    monkeypatch.setattr(loop, "time", lambda: clock["t"])
    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        """
        Record the backoff instead of waiting it out.

        :param seconds: Backoff the loop asked for.
        :returns: None.
        """
        sleeps.append(seconds)

    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _record_sleep)
    held = fwd._CODEX_ELICITATION_HELD_POLL_FLOOR_SECONDS + 290.0
    # (held_s, outcome) per attempt: a gateway sever holds the poll for its
    # full budget, a refused connection fails instantly.
    script = [
        (held, "5xx"),
        (held, "5xx"),
        (0.0, "cut"),
        (0.0, "cut"),
        (held, "5xx"),
        (0.0, "ok"),
    ]

    class _ScriptedElicitationClient:
        """Stub client whose POSTs follow *script*, advancing the fake clock."""

        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []

        async def post(self, url: str, *, json: dict, timeout: httpx.Timeout) -> httpx.Response:
            """
            Fail or succeed per the script, charging the attempt's hold time.

            :param url: Request URL.
            :param json: Codex JSON-RPC request envelope.
            :param timeout: Per-attempt budget (ignored by the stub).
            :returns: 504 for a gateway sever, 200 once the script says so.
            :raises httpx.ReadError: For an instantly refused connection.
            """
            del timeout
            self.posts.append((url, json))
            held_s, outcome = script[len(self.posts) - 1]
            clock["t"] += held_s
            request = httpx.Request("POST", url)
            if outcome == "cut":
                raise httpx.ReadError("connection refused", request=request)
            if outcome == "5xx":
                return httpx.Response(504, request=request)
            return httpx.Response(200, json={"action": "accept"}, request=request)

    client = _ScriptedElicitationClient()

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]  # stub implements the one used method
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is not None
    assert response.status_code == 200
    initial = fwd._CODEX_ELICITATION_RETRY_INITIAL_BACKOFF_SECONDS
    assert sleeps == [initial, initial, initial, initial * 2, initial * 4], (
        "a gateway-severed held poll must reset the backoff so the re-POST lands "
        "inside the server's re-park grace; only fast failures may back off"
    )


@pytest.mark.asyncio
async def test_elicitation_post_retries_gateway_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 5xx (proxy gateway error on a severed long-poll) is retried.

    The Databricks Apps proxy answers a killed upstream long-poll with
    502/504 rather than a clean transport error; the verdict may still
    be pending server-side, so the forwarder must re-park rather than
    treat it as final.
    """
    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _FlakyElicitationClient(gateway_failures=1)

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is not None
    assert response.status_code == 200
    # 2 = the 502 attempt + the successful retry; 1 would mean 5xx was
    # treated as a final answer and the prompt abandoned.
    assert len(client.posts) == 2


@pytest.mark.asyncio
async def test_elicitation_post_4xx_is_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 4xx is a deliberate server rejection — returned without retry.

    Retrying a rejection would hammer the server with a request it
    already refused; the caller logs it and leaves the native request
    unanswered.
    """

    class _RejectingClient:
        """Client stub answering every elicitation POST with HTTP 400."""

        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []

        async def post(self, url: str, *, json: dict, timeout: httpx.Timeout) -> httpx.Response:
            """
            Record the call and reject it.

            :param url: Request URL.
            :param json: Codex JSON-RPC request envelope.
            :param timeout: Per-attempt budget (ignored by the stub).
            :returns: HTTP 400.
            """
            self.posts.append((url, json))
            return httpx.Response(400, request=httpx.Request("POST", url))

    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _RejectingClient()

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is not None
    assert response.status_code == 400
    # 1 = the rejection was final; 2+ means 4xx is being retried.
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_elicitation_post_returns_none_when_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An exhausted retry budget returns ``None`` (caller leaves the
    native request unanswered, matching the old single-attempt outcome).
    """
    # Budget smaller than the first backoff → exactly one attempt.
    monkeypatch.setattr(fwd, "_CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _FlakyElicitationClient(transport_failures=100)

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is None
    # 1 = the deadline check stopped the loop before a second attempt
    # (backoff 1.0s > 0.5s budget); more means the budget is ignored.
    assert len(client.posts) == 1
