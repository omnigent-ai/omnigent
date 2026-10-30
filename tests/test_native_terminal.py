"""Tests for shared native terminal helpers."""

from __future__ import annotations

import json
from contextlib import nullcontext

import click
import httpx
import pytest

from omnigent.native import native_terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_name", ["Claude", "Codex"])
@pytest.mark.parametrize("status_code", [202, 503])
async def test_post_initial_prompt_sends_one_message(
    monkeypatch: pytest.MonkeyPatch,
    harness_name: str,
    status_code: int,
) -> None:
    """Prompt submission preserves text/auth and surfaces rejection without replay."""
    requests: list[httpx.Request] = []
    auth = httpx.BasicAuth("test", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json={"detail": "try later"})

    monkeypatch.setattr(
        "omnigent.cli_auth.open_server_client",
        lambda base_url, **kwargs: httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    with (
        pytest.raises(
            click.ClickException,
            match=(
                f"{harness_name} initial prompt failed .*try later\n"
                "Session conv/a b did not receive it"
            ),
        )
        if status_code >= 400
        else nullcontext()
    ):
        await native_terminal.post_initial_prompt(
            base_url="https://example.test",
            headers={"X-Test": "initial-prompt"},
            session_id="conv/a b",
            prompt="first line\n\n  indented line",
            auth=auth,
            harness_name=harness_name,
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.raw_path == b"/v1/sessions/conv%2Fa%20b/events"
    assert request.headers["X-Test"] == "initial-prompt"
    assert request.headers["Authorization"].startswith("Basic ")
    assert json.loads(request.content) == {
        "type": "message",
        "data": {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "first line\n\n  indented line"},
            ],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (
            httpx.ConnectError("connection refused"),
            "Claude initial prompt could not be sent (ConnectError). "
            "Session conv_abc123 did not receive it.",
        ),
        (
            httpx.ConnectTimeout(""),
            "Claude initial prompt could not be sent (ConnectTimeout). "
            "Session conv_abc123 did not receive it.",
        ),
        (
            httpx.ReadTimeout(""),
            "Claude initial prompt was not confirmed (ReadTimeout). "
            "Session conv_abc123 may not have received it.",
        ),
    ],
    ids=["connect-error", "connect-timeout", "read-timeout"],
)
async def test_post_initial_prompt_reports_transport_failure_once(
    monkeypatch: pytest.MonkeyPatch,
    error: httpx.TransportError,
    expected_message: str,
) -> None:
    """A transport failure says whether the session can have received the prompt."""
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise error

    monkeypatch.setattr(
        "omnigent.cli_auth.open_server_client",
        lambda base_url, **kwargs: httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    with pytest.raises(click.ClickException) as raised:
        await native_terminal.post_initial_prompt(
            base_url="https://example.test",
            headers={},
            session_id="conv_abc123",
            prompt="hello",
            auth=None,
            harness_name="Claude",
        )

    assert raised.value.message == expected_message
    assert len(attempts) == 1


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
async def test_bind_session_runner_retries_429_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner binding retries rate limits and honors ``Retry-After``."""
    attempts = 0
    sleeps: list[float] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Rate limit two attempts before accepting the binding."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "30"}, request=request)
        if attempts == 2:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json={}, request=request)

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(native_terminal, "_sleep", _fake_sleep)
    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")

    assert attempts == 3
    assert sleeps == [10.0, 1.0]


@pytest.mark.asyncio
async def test_bind_session_runner_surfaces_429_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final rate-limit response remains user-facing after the retry budget."""
    attempts = 0
    sleeps: list[float] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"error_code": "RESOURCE_EXHAUSTED"},
            request=request,
        )

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(native_terminal, "_sleep", _fake_sleep)
    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="RESOURCE_EXHAUSTED"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")

    assert attempts == 5
    assert sleeps == [10.0] * 4


@pytest.mark.asyncio
async def test_bind_session_runner_raises_click_exception_on_http_error() -> None:
    """
    HTTP failures surface as ClickException with server detail.

    Native wrappers call this during CLI setup, so the failure must be
    user-facing instead of leaking a raw ``httpx.Response`` shape.
    """

    attempts = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return a structured API error response.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :returns: HTTP 409 response with JSON error detail.
        """
        nonlocal attempts
        attempts += 1
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

    assert attempts == 1


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


def test_normalize_extra_args_prefers_extra_args() -> None:
    assert native_terminal.normalize_extra_args(
        extra_args=("--a",), legacy_args=None, legacy_param="pi_args"
    ) == ("--a",)


def test_normalize_extra_args_empty_when_neither_set() -> None:
    assert (
        native_terminal.normalize_extra_args(
            extra_args=None, legacy_args=None, legacy_param="pi_args"
        )
        == ()
    )


def test_normalize_extra_args_legacy_alias_warns_and_is_used() -> None:
    with pytest.warns(DeprecationWarning, match="pi_args"):
        result = native_terminal.normalize_extra_args(
            extra_args=None, legacy_args=("--legacy",), legacy_param="pi_args"
        )
    assert result == ("--legacy",)


def test_normalize_extra_args_extra_args_wins_over_legacy_with_warning() -> None:
    with pytest.warns(DeprecationWarning):
        result = native_terminal.normalize_extra_args(
            extra_args=("--new",), legacy_args=("--old",), legacy_param="pi_args"
        )
    assert result == ("--new",)
