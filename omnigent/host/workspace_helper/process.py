"""Start and stop one workspace helper process.

The helper serves the workspace API (``workspace.proto``) and its control
service (``workspace_helper.proto``) on one end of a socket pair that the host
hands it at start (``--connection-fd``). Nothing else can reach that
connection: it has no path, and the child inherits only its own end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from omnigent.host.workspace_helper.transport import SocketHTTP2Transport
from omnigent.inner._subprocess_lifecycle import close_subprocess_transport, terminate_subprocess

_logger = logging.getLogger(__name__)

# The control and workspace protocols this host speaks; see GetInfoResponse.
PROTOCOL_VERSION = 1
# `off` turns the helper off; a path names a binary, e.g. a local build.
HELPER_ENV_VAR = "OMNIGENT_WORKSPACE_HELPER"
_READY_TIMEOUT_S = 5.0
_READY_LINE_BYTES = 64 * 1024
# Each cleanup step is bounded on its own, so a stuck one can't skip the rest.
_CLOSE_WORKSPACES_TIMEOUT_S = 1.0
_TRANSPORT_CLOSE_TIMEOUT_S = 1.0
_TERMINATE_TIMEOUT_S = 2.0
_KILL_TIMEOUT_S = 2.0


class HelperUnavailable(Exception):
    """The helper could not be started now; a later call may try again."""


class HelperIncompatible(HelperUnavailable):
    """The helper can't serve this host at all, e.g. another protocol version."""


def find_helper_binary() -> str | None:
    """The helper binary to run, or ``None`` when there is none.

    :returns: ``$OMNIGENT_WORKSPACE_HELPER`` when it names a path, else the
        binary of the installed ``omnigent-workspace-helper`` package;
        ``None`` when the variable is ``off`` or nothing is installed.
    """
    configured = os.environ.get(HELPER_ENV_VAR, "").strip()
    if configured.lower() == "off":
        return None
    if configured:
        return configured
    try:
        from omnigent_workspace_helper import find_binary  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return str(find_binary())
    except (OSError, RuntimeError):
        _logger.warning("The installed workspace helper has no binary for this machine")
        return None


@dataclass
class HelperProcess:
    """A running helper and the HTTP/2 connection to it.

    :param process: The helper, leading its own process group.
    :param transport: The connection, on the host's end of the socket pair.
    :param version: The helper's release version, e.g. ``"0.12.0"``.
    :param capabilities: What it serves, as GetInfoResponse lists them.
    """

    process: asyncio.subprocess.Process
    transport: SocketHTTP2Transport
    version: str
    capabilities: frozenset[str]
    _reaping: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def running(self) -> bool:
        """Whether the helper still runs and its connection is open."""
        return self.process.returncode is None and not self.transport.lost

    async def wait_stopped(self) -> None:
        """Wait until the helper exits or its connection closes."""
        exited = asyncio.ensure_future(self.process.wait())
        closed = asyncio.ensure_future(self.transport.wait_closed())
        try:
            await asyncio.wait({exited, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            exited.cancel()
            closed.cancel()

    async def close(self, close_workspaces: Callable[[], Awaitable[None]] | None = None) -> None:
        """Stop the helper: every step runs, each within its own bound.

        :param close_workspaces: Releases the helper's open workspaces first,
            given up to a second; skipped when the helper no longer runs.
        """
        try:
            if close_workspaces is not None and self.running:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(close_workspaces(), _CLOSE_WORKSPACES_TIMEOUT_S)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.transport.close(), _TRANSPORT_CLOSE_TIMEOUT_S)
        finally:
            # Even when cancelled, the connection closes and the process is stopped and reaped.
            await asyncio.shield(self.reap())

    def reap(self) -> asyncio.Task[None]:
        """Close the connection now, then stop and reap the process; once.

        :returns: The task doing it, which outlives a cancelled caller.
        """
        if self._reaping is None:
            self.transport.abort()
            self._reaping = asyncio.ensure_future(_stop_process(self.process))
        return self._reaping


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """Close the helper's stdin, then TERM and KILL its group, and reap it."""
    if process.stdin is not None:
        with contextlib.suppress(Exception):
            process.stdin.close()
    try:
        await terminate_subprocess(
            process,
            terminate_timeout=_TERMINATE_TIMEOUT_S,
            kill_timeout=_KILL_TIMEOUT_S,
            label="workspace helper",
        )
    finally:
        close_subprocess_transport(process)


async def start_helper(binary: str) -> HelperProcess:
    """Start the helper and wait for its ready line.

    :param binary: The helper executable, e.g. from :func:`find_helper_binary`.
    :returns: The running helper, connected.
    :raises HelperIncompatible: When it speaks another protocol version.
    :raises HelperUnavailable: When it can't start, or isn't ready in time.
        Nothing it started is left running.
    """
    host_end, helper_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    process: asyncio.subprocess.Process | None = None
    transport: SocketHTTP2Transport | None = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "--connection-fd",
                str(helper_end.fileno()),
                # The helper exits when the host does, which closes this pipe.
                "--exit-on-stdin-eof",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(helper_end.fileno(),),
                start_new_session=True,
            )
        except OSError as exc:
            raise HelperUnavailable(f"The workspace helper could not start: {exc}") from exc
        finally:
            # Only the child keeps its end; the host holding it would hide the child's exit.
            helper_end.close()
        assert process.stdout is not None
        ready = await _read_ready_line(process.stdout)
        if ready.get("protocolVersion") != PROTOCOL_VERSION:
            raise HelperIncompatible(
                f"The workspace helper speaks protocol {ready.get('protocolVersion')!r},"
                f" not {PROTOCOL_VERSION}"
            )
        transport = SocketHTTP2Transport(host_end)
        await transport.start()
        capabilities = ready.get("capabilities")
        if not isinstance(capabilities, list):
            capabilities = []
        return HelperProcess(
            process=process,
            transport=transport,
            version=str(ready.get("version", "")),
            capabilities=frozenset(name for name in capabilities or [] if isinstance(name, str)),
        )
    except BaseException:
        # Includes cancellation: a half-started helper is never left behind.
        abandoning = asyncio.ensure_future(_abandon(process, transport, host_end))
        _ABANDONING.add(abandoning)
        abandoning.add_done_callback(_ABANDONING.discard)
        await asyncio.shield(abandoning)
        raise


# Cleanups of failed starts; held here so a cancelled start can't drop one.
_ABANDONING: set[asyncio.Task[None]] = set()


async def wait_for_abandoned_starts() -> None:
    """Wait until every failed start has stopped and reaped its process."""
    while _ABANDONING:
        await asyncio.wait(set(_ABANDONING))


async def _read_ready_line(stdout: asyncio.StreamReader) -> dict[str, object]:
    try:
        line = await asyncio.wait_for(stdout.readline(), _READY_TIMEOUT_S)
    except TimeoutError as exc:
        raise HelperUnavailable("The workspace helper didn't report ready in time") from exc
    except ValueError as exc:
        raise HelperUnavailable("The workspace helper's ready line is too long") from exc
    if len(line) > _READY_LINE_BYTES or not line:
        raise HelperUnavailable("The workspace helper exited before it was ready")
    try:
        ready = json.loads(line)
    except ValueError as exc:
        raise HelperUnavailable("The workspace helper's ready line isn't JSON") from exc
    if not isinstance(ready, dict) or ready.get("ready") is not True:
        raise HelperUnavailable("The workspace helper didn't report ready")
    return ready


async def _abandon(
    process: asyncio.subprocess.Process | None,
    transport: SocketHTTP2Transport | None,
    host_end: socket.socket,
) -> None:
    if transport is not None:
        transport.abort()
    else:
        host_end.close()
    if process is not None:
        await _stop_process(process)
