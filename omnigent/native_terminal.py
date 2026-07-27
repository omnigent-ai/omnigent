"""Shared helpers for native terminal wrappers."""

from __future__ import annotations

import urllib.parse

import click
import httpx

from omnigent.host.daemon_launch import error_text

DAEMON_HOST_ONLINE_TIMEOUT_S = 30.0
DAEMON_RUNNER_ONLINE_TIMEOUT_S = 60.0
DAEMON_TERMINAL_READY_TIMEOUT_S = 60.0


def url_component(value: str) -> str:
    """
    Percent-encode a value for a path component.

    :param value: Raw path component, e.g. ``"conv/abc"``.
    :returns: Encoded path component, e.g. ``"conv%2Fabc"``.
    """
    return urllib.parse.quote(value, safe="")


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
        resp = await client.patch(
            f"/v1/sessions/{url_component(session_id)}",
            json={"runner_id": runner_id},
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
