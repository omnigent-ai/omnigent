"""Runner-owned bearer refresh for the native Pi extension's bridge config."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from omnigent.cli_auth import databricks_request_headers
from omnigent.harnesses.pi_native.auth_command import supervise_model_auth
from omnigent.harnesses.pi_native.bridge import refresh_config_auth_headers

_logger = logging.getLogger(__name__)

# Revisit cached tokens within the SDK's 40-second renewal margin;
# stored OIDC tokens have a wider 90-second margin.
PI_AUTH_REFRESH_INTERVAL_S = 30.0
PI_AUTH_REFRESH_TIMEOUT_S = 30.0


async def supervise_pi_credentials(
    bridge_dir: Path,
    server_url: str,
    *,
    auth_dir: Path | None,
    refresh_bridge: bool,
) -> None:
    """Keep model and bridge credentials under the runner's existing lifecycle."""
    async with asyncio.TaskGroup() as tasks:
        if auth_dir is not None:
            tasks.create_task(supervise_model_auth(auth_dir))
        if refresh_bridge:
            tasks.create_task(supervise_pi_auth_refresh(bridge_dir, server_url))


async def supervise_pi_auth_refresh(
    bridge_dir: Path,
    server_url: str,
    *,
    interval_s: float = PI_AUTH_REFRESH_INTERVAL_S,
    timeout_s: float = PI_AUTH_REFRESH_TIMEOUT_S,
) -> None:
    """
    Re-mint the extension's bearer every *interval_s* until cancelled.

    Turns typed in the Pi TUI never reach the executor's per-turn refresh, so
    without this the launch bearer expires and event POSTs fail with 403. The
    host bootstrap bearer is skipped: it is only replaced after an HTTP
    rejection, which this task never triggers. Failures keep the existing
    headers and retry next interval; a mint finishing after cancellation is
    discarded.

    :param bridge_dir: Native Pi bridge directory holding ``config.json``.
    :param server_url: Omnigent server URL the extension posts to.
    :param interval_s: Seconds between refreshes.
    :param timeout_s: Deadline for one credential resolution or mint.
    """
    from omnigent.runner._entry import _make_auth_token_factory

    factory = None
    failing = False
    while True:
        try:
            if factory is None:
                factory = await asyncio.wait_for(
                    asyncio.to_thread(
                        _make_auth_token_factory, server_url, _allow_initial_token=False
                    ),
                    timeout_s,
                )
            token = (
                await asyncio.wait_for(asyncio.to_thread(factory), timeout_s) if factory else None
            )
            if token:
                headers = databricks_request_headers(server_url, bearer_token=token)
                refresh_config_auth_headers(bridge_dir, headers)
                failing = False
            else:
                # A managed mint can latch a temporary refusal without raising.
                # Re-resolve on the next interval so that latch cannot stick.
                factory = None
        except Exception as error:  # noqa: BLE001 — best-effort; keep headers, retry
            factory = None
            if not failing:  # Provider messages can embed secrets: log the type only.
                _logger.warning("Pi bridge auth refresh failed (%s)", type(error).__name__)
            failing = True
        await asyncio.sleep(interval_s)
