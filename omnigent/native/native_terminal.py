"""Shared helpers for native terminal wrappers."""

from __future__ import annotations

import asyncio
import urllib.parse
import warnings
from collections.abc import Awaitable, Callable

import click
import httpx

from omnigent.host.daemon_launch import error_text
from omnigent.util.http_retry import bounded_retry_after_seconds

DAEMON_HOST_ONLINE_TIMEOUT_S = 30.0
DAEMON_RUNNER_ONLINE_TIMEOUT_S = 60.0
DAEMON_TERMINAL_READY_TIMEOUT_S = 60.0
_HTTP_429_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0)
_HTTP_429_MAX_RETRY_AFTER_S = 10.0
_sleep = asyncio.sleep


async def request_with_429_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    delays_s: tuple[float, ...] = _HTTP_429_RETRY_DELAYS_S,
    max_retry_after_s: float = _HTTP_429_MAX_RETRY_AFTER_S,
) -> httpx.Response:
    """Send an HTTP request, retrying only explicit 429 responses.

    The callable rebuilds the request for every attempt, which keeps multipart
    session creation bodies replayable. ``Retry-After`` takes precedence over
    the bounded exponential fallback schedule.

    Retries are bounded by attempt count, not a wall-clock deadline: with
    every hint capped, the sleeps total at most
    ``len(delays_s) * max_retry_after_s`` (40s with the defaults).

    :param send: Callable that creates and sends one request attempt.
    :param delays_s: Fallback pause before each retry when the response has
        no usable ``Retry-After`` hint, e.g. ``(0.5, 1.0, 2.0)``.
    :param max_retry_after_s: Cap on any single pause, e.g. ``10.0``.
    :returns: The first non-429 response, or the final 429 response.
    """
    for fallback_delay_s in delays_s:
        response = await send()
        if response.status_code != 429:
            return response
        await _sleep(
            bounded_retry_after_seconds(
                response,
                fallback=fallback_delay_s,
                max_delay=max_retry_after_s,
            )
        )
    return await send()


def normalize_extra_args(
    *,
    extra_args: tuple[str, ...] | None,
    legacy_args: tuple[str, ...] | None,
    legacy_param: str,
) -> tuple[str, ...]:
    """Reconcile a native launcher's pass-through args from both spellings.

    Every ``run_<x>_native`` entry point accepts a uniform keyword-only
    ``extra_args`` (the spelling the provider seam calls with) plus its legacy
    ``<x>_args`` alias. This collapses the two into one tuple:

    - only ``extra_args`` set → use it;
    - only the legacy alias set → use it, emitting a ``DeprecationWarning``;
    - both set → ``extra_args`` wins (the legacy alias is ignored) with a warning;
    - neither set → empty tuple.

    The ``<x>_args`` alias is deprecated and slated for removal in 0.9.0; callers
    should pass ``extra_args``.
    """
    if legacy_args is not None:
        warnings.warn(
            f"{legacy_param!r} is deprecated; pass extra_args instead "
            "(the alias is removed in 0.9.0)",
            DeprecationWarning,
            stacklevel=3,
        )
        if extra_args is None:
            return tuple(legacy_args)
    return tuple(extra_args or ())


def url_component(value: str) -> str:
    """
    Percent-encode a value for a path component.

    :param value: Raw path component, e.g. ``"conv/abc"``.
    :returns: Encoded path component, e.g. ``"conv%2Fabc"``.
    """
    return urllib.parse.quote(value, safe="")


async def post_initial_prompt(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    prompt: str,
    auth: httpx.Auth | None,
    harness_name: str,
) -> None:
    """Queue a prompt as a message rather than persisting it in launch args.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers for Omnigent requests.
    :param session_id: Prepared session id, e.g. ``"conv_abc123"``.
    :param prompt: User prompt text, including any newlines.
    :param auth: Optional refresh-capable HTTP auth.
    :param harness_name: Display name for errors, e.g. ``"Claude"``.
    :returns: None.
    :raises click.ClickException: If the prompt is rejected or cannot be sent.
    """
    from omnigent.cli_auth import open_server_client

    unsent = f"Session {session_id} did not receive it."
    try:
        async with open_server_client(
            base_url,
            headers=headers,
            auth=auth,
            timeout=httpx.Timeout(30.0),
        ) as client:
            response = await client.post(
                f"/v1/sessions/{url_component(session_id)}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                },
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise click.ClickException(
            f"{harness_name} initial prompt could not be sent ({type(exc).__name__}). {unsent}"
        ) from exc
    except httpx.HTTPError as exc:
        # The request may have reached the server, which can still queue it.
        raise click.ClickException(
            f"{harness_name} initial prompt was not confirmed ({type(exc).__name__}). "
            f"Session {session_id} may not have received it."
        ) from exc
    if response.status_code >= 400:
        raise click.ClickException(
            f"{harness_name} initial prompt failed ({response.status_code}): "
            f"{error_text(response)}\n{unsent}"
        )


def terminal_attach_url(base_url: str, session_id: str, terminal_id: str) -> str:
    """
    Build the terminal attach WebSocket URL.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param session_id: Session/conversation id, e.g.
        ``"conv_abc123"``.
    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_claude_main"``.
    :returns: Fully-qualified attach WebSocket URL.
    """
    parsed = urllib.parse.urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    path = (
        f"{base_path}/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}/attach"
    )
    return urllib.parse.urlunsplit((scheme, parsed.netloc, path, "", ""))


async def bind_session_runner(
    client: httpx.AsyncClient,
    session_id: str,
    runner_id: str,
) -> None:
    """
    Bind a native terminal session to the runner that will host it.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session/conversation id, e.g.
        ``"conv_abc123"``.
    :param runner_id: Registered runner id, e.g.
        ``"runner_abc123"``.
    :returns: None.
    :raises click.ClickException: If binding fails.
    """
    try:
        resp = await request_with_429_retry(
            lambda: client.patch(
                f"/v1/sessions/{url_component(session_id)}",
                json={"runner_id": runner_id},
            )
        )
    except httpx.ConnectError as exc:
        # Connection refused/reset or DNS failure: the server was never reached.
        raise click.ClickException(
            f"Couldn't reach the Omnigent server to bind session {session_id!r} ({exc!r}). "
            "Check the server URL and your connection."
        ) from exc
    except httpx.ConnectTimeout as exc:
        # Connect phase timed out: also never reached the server (down or wrong
        # host), so point at the URL/connection rather than server load.
        raise click.ClickException(
            f"Couldn't reach the Omnigent server to bind session {session_id!r}: connection "
            f"timed out ({exc!r}). Check the server URL and your connection."
        ) from exc
    except httpx.TimeoutException as exc:
        # Connected, but the server didn't respond in time: it's reachable and
        # likely slow rather than down.
        raise click.ClickException(
            f"Timed out binding session {session_id!r}: the Omnigent server is responding too "
            f"slowly ({exc!r}); retry shortly."
        ) from exc
    except httpx.TransportError as exc:
        # Any other transport failure (protocol error, etc.): no response, so
        # there is no status to report.
        raise click.ClickException(
            f"Couldn't reach the Omnigent server to bind session {session_id!r} ({exc!r}). "
            "Check the server URL and your connection."
        ) from exc
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Native terminal session runner bind failed ({resp.status_code}): {error_text(resp)}"
        )
