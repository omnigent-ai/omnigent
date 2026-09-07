"""Shared helpers for native terminal wrappers."""

from __future__ import annotations

import urllib.parse
import warnings

import click
import httpx

from omnigent.host.daemon_launch import error_text

DAEMON_HOST_ONLINE_TIMEOUT_S = 30.0
DAEMON_RUNNER_ONLINE_TIMEOUT_S = 60.0
DAEMON_TERMINAL_READY_TIMEOUT_S = 60.0


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
