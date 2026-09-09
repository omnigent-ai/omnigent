"""Hostile-wire tests for the signer-owned model relay."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner.model_egress import FrozenModelRoute
from omnigent.inner.model_signer_service import (
    _MAX_BODY_BYTES,
    _parse_strict_request_headers,
    _parse_strict_request_line,
    _SignerRelay,
)
from omnigent.inner.model_signing import SigningRejected

_ROUTE = FrozenModelRoute(method="POST", host="model.test", path="/v1/responses")


class _Writer:
    def __init__(self, *, disconnect: bool = False) -> None:
        self.data = bytearray()
        self.disconnect = disconnect

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        if self.disconnect:
            raise ConnectionResetError


class _BlockingWriter(_Writer):
    async def drain(self) -> None:
        await asyncio.Future()


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


@pytest.mark.parametrize(
    "line",
    [
        b"POST /v1/responses HTTP/1.0\r\n",
        b"POST /v1/responses HTTP/2\r\n",
        b"POST  /v1/responses HTTP/1.1\r\n",
        b"POST /v1/responses HTTP/1.1 EXTRA\r\n",
        b"POST /v1/responses HTTP/1.1\n",
        b"POST\thttps://model.test/v1/responses HTTP/1.1\r\n",
        b"POST https://model.test/v1/responses HTTP/1.1\r\n",
        b"POST /v1/responses?x=1 HTTP/1.1\r\n",
        b"POST /v1/responses\x00 HTTP/1.1\r\n",
    ],
)
def test_request_line_requires_exact_http11_origin_form(line: bytes) -> None:
    with pytest.raises(SigningRejected):
        _parse_strict_request_line(line, _ROUTE)


def test_request_line_accepts_only_the_frozen_route() -> None:
    assert _parse_strict_request_line(b"POST /v1/responses HTTP/1.1\r\n", _ROUTE) == (
        "POST",
        "/v1/responses",
    )


@pytest.mark.parametrize(
    "headers",
    [
        b"Host: model.test\nContent-Length: 0\n\n",
        b"Host: model.test\r\n folded: value\r\nContent-Length: 0\r\n\r\n",
        b"Host : model.test\r\nContent-Length: 0\r\n\r\n",
        b"Bad Name: value\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nX-Test: bad\x00value\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nContent-Length: +1\r\n\r\n",
        b"Host: model.test\r\nContent-Length: -1\r\n\r\n",
        b"Host: model.test\r\nContent-Length: 1 \r\n\r\n",
        b"Host: model.test\r\nContent-Length: 999999999999999999999\r\n\r\n",
        b"Host: model.test\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"Host: model.test\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nProxy-Connection: keep-alive\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nUpgrade: websocket\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nExpect: 100-continue\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nX-Forwarded-Host: evil.test\r\nContent-Length: 0\r\n\r\n",
        b"Host: model.test\r\nContent-Length: 0\r\n",
    ],
)
def test_request_headers_reject_ambiguous_or_malformed_framing(headers: bytes) -> None:
    with pytest.raises(SigningRejected):
        _parse_strict_request_headers(headers)


def test_request_headers_reject_oversized_block() -> None:
    headers = b"X-Fill: " + (b"a" * 65_536) + b"\r\n\r\n"
    with pytest.raises(SigningRejected):
        _parse_strict_request_headers(headers)


def test_request_headers_accept_canonical_bounded_content_length() -> None:
    parsed, length = _parse_strict_request_headers(
        b"Host: model.test\r\n"
        b"Authorization: Bearer oa_cred_test\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n\r\n"
    )
    assert length == 2
    assert ("Content-Length", "2") in parsed


def test_request_headers_accept_and_strip_exact_keep_alive() -> None:
    parsed, length = _parse_strict_request_headers(
        b"Host: model.test\r\nConnection: keep-alive\r\nContent-Length: 0\r\n\r\n"
    )
    assert length == 0
    assert all(name.lower() != "connection" for name, _ in parsed)


def test_request_headers_reject_body_over_limit_before_read() -> None:
    with pytest.raises(SigningRejected):
        _parse_strict_request_headers(
            f"Host: model.test\r\nContent-Length: {_MAX_BODY_BYTES + 1}\r\n\r\n".encode()
        )


async def test_response_strips_sensitive_and_hop_headers_and_reframes() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    upstream = _reader(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n"
        b"Set-Cookie: secret=1\r\n"
        b"Set-Cookie: other=2\r\n"
        b"Proxy-Authenticate: Basic secret\r\n"
        b"Connection: keep-alive\r\n"
        b"X-Internal-Secret: nope\r\n\r\n{}"
    )
    client = _Writer()

    relayed, status, keep_alive = await relay._relay_response_observing_status(upstream, client)

    assert status == 200
    assert relayed > 0
    assert keep_alive
    assert bytes(client.data) == (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: keep-alive\r\n\r\n{}"
    )


async def test_chunked_sse_is_decoded_and_streamed_without_te_or_cl() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    upstream = _reader(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"5\r\ndata:\r\n5\r\n ok\n\n\r\n0\r\n\r\n"
    )
    client = _Writer()

    relayed, status, keep_alive = await relay._relay_response_observing_status(upstream, client)

    assert status == 200
    assert not keep_alive
    response = bytes(client.data)
    assert b"Transfer-Encoding" not in response
    assert b"Content-Length" not in response
    assert response.endswith(b"\r\n\r\ndata: ok\n\n")
    assert relayed == len(response)


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 200 OK\nContent-Length: 0\n\n",
        b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 20 OK\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n folded: bad\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: +1\r\n\r\n",
    ],
)
async def test_malformed_upstream_response_becomes_no_relay(response: bytes) -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()

    relayed, status, keep_alive = await relay._relay_response_observing_status(
        _reader(response), client
    )

    assert (relayed, status, keep_alive) == (0, 0, False)
    assert not client.data


async def test_oversized_response_headers_are_rejected() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    response = b"HTTP/1.1 200 OK\r\nX-Fill: " + (b"a" * 65_536) + b"\r\n\r\n"
    client = _Writer()

    assert await relay._relay_response_observing_status(_reader(response), client) == (
        0,
        0,
        False,
    )
    assert not client.data


async def test_redirect_is_returned_without_an_upstream_follow() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()
    upstream = _reader(
        b"HTTP/1.1 307 Temporary Redirect\r\n"
        b"Location: https://other.test/path\r\n"
        b"Content-Length: 0\r\n\r\n"
    )

    _, status, keep_alive = await relay._relay_response_observing_status(upstream, client)

    assert status == 307
    assert keep_alive
    assert b"Location: https://other.test/path\r\n" in client.data


async def test_interim_response_is_consumed_until_final_response() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()
    upstream = _reader(
        b"HTTP/1.1 103 Early Hints\r\n"
        b"Link: </model>; rel=preload\r\n\r\n"
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n\r\n{}"
    )

    relayed, status, keep_alive = await relay._relay_response_observing_status(upstream, client)

    assert status == 200
    assert keep_alive
    assert relayed == len(client.data)
    assert bytes(client.data).startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"103 Early Hints" not in client.data


async def test_switching_protocols_is_rejected_without_relay() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()
    upstream = _reader(
        b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
    )

    assert await relay._relay_response_observing_status(upstream, client) == (0, 0, False)
    assert not client.data


async def test_excessive_interim_responses_are_rejected() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()
    upstream = _reader(
        b"HTTP/1.1 103 Early Hints\r\n\r\n" * 9 + b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    )

    assert await relay._relay_response_observing_status(upstream, client) == (0, 0, False)
    assert not client.data


async def test_client_disconnect_aborts_response_stream() -> None:
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer(disconnect=True)
    upstream = _reader(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n5\r\ndata:\r\n0\r\n\r\n"
    )

    with pytest.raises(ConnectionResetError):
        await relay._relay_response_observing_status(upstream, client)


async def test_idle_response_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.inner.model_signer_service._RESPONSE_IDLE_TIMEOUT_SECONDS",
        0.001,
    )
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()

    assert await relay._relay_response_observing_status(asyncio.StreamReader(), client) == (
        0,
        0,
        False,
    )
    assert not client.data


async def test_total_response_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.inner.model_signer_service._RESPONSE_TOTAL_TIMEOUT_SECONDS",
        0.0,
    )
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()

    result = await relay._relay_response_observing_status(
        _reader(b"HTTP/1.1 200 OK\r\n"),
        client,
    )
    assert result == (
        0,
        0,
        False,
    )
    assert not client.data


async def test_total_response_timeout_bounds_client_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.inner.model_signer_service._RESPONSE_TOTAL_TIMEOUT_SECONDS",
        0.001,
    )
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()

    with pytest.raises(asyncio.TimeoutError):
        await relay._relay_response_observing_status(
            _reader(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
            _BlockingWriter(),
        )


async def test_unframed_infinite_response_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.inner.model_signer_service._MAX_RESPONSE_BODY_BYTES", 4)
    relay = object.__new__(_SignerRelay)
    relay._credential_source = Mock()
    client = _Writer()

    relayed, status, keep_alive = await relay._relay_response_observing_status(
        _reader(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n12345"),
        client,
    )

    assert status == 200
    assert not keep_alive
    assert relayed == bytes(client.data).index(b"\r\n\r\n") + 4
    assert bytes(client.data).endswith(b"\r\n\r\n")


@pytest.mark.parametrize("sni", [None, "attacker.test"])
async def test_missing_or_mismatched_sni_is_rejected(sni: str | None) -> None:
    relay = object.__new__(_SignerRelay)
    relay._route = _ROUTE
    relay._placeholder = "oa_cred_test"
    relay._provider_port = 443
    relay._credential_source = Mock(token_for_request=AsyncMock(return_value="real-token"))
    relay._tls_sni_by_task = {}
    task = asyncio.current_task()
    assert task is not None
    relay._tls_sni_by_task[task] = sni
    client = _Writer()

    await relay._forward_https(
        client,  # type: ignore[arg-type]
        "model.test",
        443,
        "POST",
        "/v1/responses",
        b"POST /v1/responses HTTP/1.1\r\n",
        b"Host: model.test\r\n"
        b"Authorization: Bearer oa_cred_test\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n\r\n",
        b"{}",
    )

    assert bytes(client.data).startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert b"attacker.test" not in client.data
    relay._credential_source.token_for_request.assert_not_awaited()


async def test_upstream_401_is_observed_without_replay() -> None:
    credential = Mock(token_for_request=AsyncMock())
    relay = object.__new__(_SignerRelay)
    relay._credential_source = credential
    client = _Writer()

    _, status, keep_alive = await relay._relay_response_observing_status(
        _reader(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"),
        client,
    )

    assert status == 401
    assert keep_alive
    credential.invalidate_after_unauthorized.assert_called_once_with()
    credential.token_for_request.assert_not_awaited()
