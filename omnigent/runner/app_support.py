"""Small helpers shared by the runner app and the modules split out of it."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Literal, TypeAlias, overload

from omnigent.debug_logging import runner_primary_session_id
from omnigent.process_logging import process_log_reference
from omnigent.runner.native import ResolvedSpec
from omnigent.spec.types import AgentSpec
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger("omnigent.runner.app")


def _client_safe_error_detail(exc: BaseException, *, context: str) -> str:
    """
    Log *exc* in full and return a generic detail string safe for clients.

    Raw exception text (``str(exc)``) can embed absolute paths, internal
    hostnames, PIDs, and other server-side state. The runner is reached via
    the AP server proxy and its error bodies are relayed to the caller, so
    the cause is logged here for operators while the HTTP response carries
    only this fixed string. The structured ``error`` code that accompanies
    the detail already names the failure category for the caller.

    The runner's own log path is named so the reader can go read the cause
    instead of hunting for it; it is home-relative (``~/…``) so it points
    somewhere without leaking the account name.

    :param exc: The caught exception, e.g. a ``RuntimeError`` from a harness
        spawn or an ``InvalidPath`` from path validation.
    :param context: Short operator-facing label for the failing operation,
        e.g. ``"harness spawn"``. Appears only in the server log.
    :returns: A non-sensitive string safe to return to clients, e.g.
        ``"Request failed on the runner; see the runner log for details:
        ~/.omnigent/logs/runner/runner-conv_ab12.log"``.
    """
    _logger.warning(
        "%s failed: %s",
        context,
        exc,
        exc_info=exc,
        extra={"session_id": runner_primary_session_id()},
    )
    log_reference = process_log_reference("runner")
    return f"Request failed on the runner; see the runner log for details: {log_reference}"


_SpecEntry: TypeAlias = AgentSpec | ResolvedSpec
SpecResolver: TypeAlias = Callable[[str, str | None], Awaitable[_SpecEntry | None]]
_ResourceType: TypeAlias = Literal["environment", "terminal", "file"]


@overload
def _unwrap_spec_entry(entry: None) -> None: ...


@overload
def _unwrap_spec_entry(entry: _SpecEntry) -> AgentSpec: ...


def _unwrap_spec_entry(entry: _SpecEntry | None) -> AgentSpec | None:
    """Return the agent spec from a runner app cache entry."""
    return entry.spec if isinstance(entry, ResolvedSpec) else entry


class _BodyRequest:
    """Minimal stand-in for a Starlette ``Request`` exposing only ``json()``.

    Lets internal callers reuse a route handler that consumes the request
    solely for its JSON body (e.g. ``create_session_terminal``) without
    constructing a real ASGI ``Request``. Not a general Request substitute.
    """

    def __init__(self, body: _JsonObject) -> None:
        self._body = body

    async def json(self) -> _JsonObject:
        return self._body
