"""Tests for the UDS runner-transport builder used by server startup."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.server import _runner_transport as rt


def test_build_uds_runner_returns_uds_client(tmp_path: Any) -> None:
    """The HTTP client is wired to the cosmetic ``http://runner`` base URL."""
    sock_path = str(tmp_path / "runner.sock")
    client, _factory = rt.build_uds_runner(sock_path)
    try:
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)
        assert client.base_url == httpx.URL("http://runner")
    finally:
        # We never open a connection in this test; clearing the
        # transport internals avoids unclosed-client warnings without
        # needing an async fixture just for bookkeeping.
        client._transport.__dict__.clear()


def test_build_uds_runner_ws_factory_uses_unix_connect(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WS factory invokes ``websockets.unix_connect`` with the UDS path."""
    sock_path = str(tmp_path / "runner.sock")
    captured: dict[str, Any] = {}

    def fake_unix_connect(
        path: str | None = None,
        uri: str | None = None,
        **kwargs: Any,
    ) -> Any:
        captured["path"] = path
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return "FAKE_CM"

    monkeypatch.setattr(
        "websockets.asyncio.client.unix_connect",
        fake_unix_connect,
    )

    _client, factory = rt.build_uds_runner(sock_path)
    result = factory(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?read_only=false"
    )

    assert result == "FAKE_CM"
    assert captured["path"] == sock_path
    assert captured["uri"] == (
        "ws://runner/v1/sessions/conv_abc/resources/terminals/"
        "terminal_bash_s1/attach?read_only=false"
    )
    assert captured["kwargs"].get("open_timeout") == 10
