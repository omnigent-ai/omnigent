"""Tests for shared native terminal helpers."""

from __future__ import annotations

import click
import httpx
import pytest

from omnigent import native_terminal


@pytest.mark.asyncio
async def test_bind_session_runner_patches_encoded_session_path() -> None:
    """
    ``bind_session_runner`` patches the encoded session resource.

    A regression here would either bind the wrong session when ids
    contain path separators, or drop the ``runner_id`` body that the
    Omnigent server expects before launching a native terminal.
    """
    seen: dict[str, object] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the outbound request sent by the helper.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :returns: Successful empty JSON response.
        """
        seen["method"] = request.method
        seen["path"] = request.url.raw_path
        seen["json"] = request.read()
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        await native_terminal.bind_session_runner(client, "conv/a b", "runner_abc")

    assert seen["method"] == "PATCH"
    assert seen["path"] == b"/v1/sessions/conv%2Fa%20b"
    assert seen["json"] == b'{"runner_id":"runner_abc"}'


@pytest.mark.asyncio
async def test_bind_session_runner_raises_click_exception_on_http_error() -> None:
    """
    HTTP failures surface as ClickException with server detail.

    Native wrappers call this during CLI setup, so the failure must be
    user-facing instead of leaking a raw ``httpx.Response`` shape.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return a structured API error response.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :returns: HTTP 409 response with JSON error detail.
        """
        return httpx.Response(
            409,
            json={"detail": "runner already bound"},
            request=request,
        )

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="runner already bound"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")


@pytest.mark.asyncio
async def test_bind_session_runner_raises_click_exception_on_timeout() -> None:
    """
    Read timeouts surface as a "server overloaded" ClickException.

    When the server connects but is too slow to answer, the PATCH raises
    before any response exists. A timeout means reachable-but-slow, so the
    message must say to retry rather than a raw ``httpx.ReadTimeout`` trace.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Simulate the server never sending response headers.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :raises httpx.ReadTimeout: Always, to model a slow/overloaded server.
        """
        raise httpx.ReadTimeout("read timed out", request=request)

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="responding too slowly"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")


@pytest.mark.asyncio
async def test_bind_session_runner_raises_click_exception_on_connect_error() -> None:
    """
    Connection failures surface as an "unreachable" ClickException.

    A connect error (bad URL, reset, DNS failure) means the server was
    never reached, so the message points at the URL/connection rather
    than leaking a raw ``httpx.ConnectError`` stack trace.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Simulate a failed connection attempt.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :raises httpx.ConnectError: Always, to model an unreachable server.
        """
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="Couldn't reach the Omnigent server"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")


@pytest.mark.asyncio
async def test_bind_session_runner_treats_connect_timeout_as_unreachable() -> None:
    """
    Connect timeouts point at the URL/connection, not server load.

    ``httpx.ConnectTimeout`` subclasses ``TimeoutException``, but the
    connect phase timing out means the server was never reached, so it
    must not use the "responding too slowly" message.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Simulate the connect phase timing out.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :raises httpx.ConnectTimeout: Always, to model an unreachable server.
        """
        raise httpx.ConnectTimeout("connect timed out", request=request)

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="Couldn't reach the Omnigent server"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")


def test_terminal_attach_url_encodes_path_components_and_switches_scheme() -> None:
    """
    Attach URLs preserve base paths and percent-encode ids.

    This is the shared helper behind the Claude and Codex wrapper-local
    ``_attach_url`` aliases, so a bad URL here breaks both native
    terminal attach paths.
    """
    url = native_terminal.terminal_attach_url(
        "https://example.databricks.com/base/",
        "conv/a b",
        "terminal/main",
    )

    assert (
        url == "wss://example.databricks.com/base/v1/sessions/conv%2Fa%20b"
        "/resources/terminals/terminal%2Fmain/attach"
    )
