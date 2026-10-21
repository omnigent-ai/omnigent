"""Pick where a remote-server session's runner launches when none is online."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from omnigent_client import OmnigentError

from omnigent.cli_diagnostics import printable
from omnigent.cli_invocation import cli_invocation

if TYPE_CHECKING:
    from omnigent_client import SessionsNamespace

logger = logging.getLogger(__name__)


def no_online_runner_message() -> str:
    """Explain how to connect a runner using the current CLI invocation."""
    command = cli_invocation()
    return (
        "This server has no online runner to run the turn. Start one against "
        f"it with `{command} host --server <url>` (or run the agent locally "
        f"with `{command} run <agent.yaml>`), then retry."
    )


class LaunchHostError(RuntimeError):
    """Host discovery or launch selection failed; ``str()`` explains why."""


class RemoteLaunchHostError(LaunchHostError):
    """Only other machines' hosts can launch the runner, and none can use this cwd."""

    def __init__(self, host_names: list[str]) -> None:
        names = ", ".join(printable(name) for name in host_names)
        super().__init__(
            f"No runner is online, and the connected hosts ({names}) are other machines "
            "that can't use this directory as their workspace. Run this command on one of "
            f"them, or connect this machine with `{cli_invocation()} host --server <url>`."
        )
        self.host_names = host_names


class LocalHostCannotLaunchError(LaunchHostError):
    """This machine is a connected host but can't run the agent's harness."""

    def __init__(self, harness: str | None, reason: str | None) -> None:
        from omnigent.onboarding.harness_install import harness_setup_hint

        super().__init__(
            f"No runner is online, and this machine's host can't run "
            f"{printable(harness or 'this agent')} ({printable(reason or 'unavailable')}): "
            f"{harness_setup_hint(harness)}, then restart `{cli_invocation()} host`."
        )


async def resolve_remote_launch_host(
    sessions: SessionsNamespace, *, harness: str | None
) -> str | None:
    """
    Return this machine's host id if the server can launch a runner on it.

    Only this machine qualifies: the session's workspace is this process's cwd,
    which another machine's host can't open. ``None`` when no host is connected
    or an older server does not expose the hosts route.

    :raises LaunchHostError: If host discovery fails on a supported route.
    :raises LocalHostCannotLaunchError: If this machine's host can't run *harness*.
    :raises RemoteLaunchHostError: If only other machines' hosts could.
    """
    from omnigent.host import identity

    try:
        hosts = await sessions.list_launch_hosts(harness=harness)
    except OmnigentError as exc:
        if exc.status_code == 404:
            logger.debug("server does not expose launch hosts", exc_info=True)
            return None
        status = f" (HTTP {exc.status_code})" if exc.status_code is not None else ""
        raise LaunchHostError(
            f"Could not list connected hosts: GET /v1/hosts failed{status}. "
            "Check the server and your access, then retry."
        ) from exc
    local = identity.load_host_identity_if_present()
    mine = next((h for h in hosts if local is not None and h.host_id == local.host_id), None)
    if mine is not None:
        if mine.launchable:
            return mine.host_id
        raise LocalHostCannotLaunchError(harness, mine.unavailable_reason)
    others = [h.name for h in hosts if h.launchable]
    if others:
        raise RemoteLaunchHostError(others)
    return None
