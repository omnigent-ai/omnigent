"""Periodic cleanup of stale and pending server-managed sandboxes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.server.managed_hosts import (
    ManagedSandboxDeployment,
    _launcher_for_teardown,
)
from omnigent.stores.host_store import Host, HostStore, host_is_live

_logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 24 * 60 * 60


class ManagedSandboxReaper:
    """Reap stale generations and retry pending provider cleanup.

    Each loop covers the whole configured deployment. A sweep discovers every
    workspace containing an active or pending managed sandbox, then queries
    candidates within that workspace. Offline age comes from the host heartbeat
    row, which is the persisted source of truth for when the sandbox was last
    online.

    Reaping detaches only the stale provider generation. The session transcript
    and durable host row remain, allowing a fresh sandbox to launch while failed
    provider cleanup stays pending for a later sweep.
    """

    def __init__(
        self,
        *,
        host_store: HostStore,
        sandbox_config: ManagedSandboxDeployment,
        clock: Callable[[], int] = now_epoch,
    ) -> None:
        self._host_store = host_store
        self._sandbox_config = sandbox_config
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start this server process's deployment-wide reaper loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(),
            name="managed-sandbox-reaper",
        )

    async def shutdown(self) -> None:
        """Stop the reaper loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def sweep_once(self, *, now: int | None = None) -> int:
        """Run one complete cross-workspace sweep and return the reap count."""
        reference_time = self._clock() if now is None else now
        workspace_ids = await asyncio.to_thread(
            self._host_store.list_managed_sandbox_workspace_ids
        )
        reaped = 0
        for workspace_id in workspace_ids:
            try:
                reaped += await self._sweep_current_workspace(
                    workspace_id,
                    reference_time,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "Managed sandbox reaper failed for workspace %s; continuing",
                    workspace_id,
                )
        return reaped

    async def _run(self) -> None:
        while True:
            try:
                reaped = await self.sweep_once()
                if reaped:
                    _logger.info("Managed sandbox reaper terminated %s generation(s)", reaped)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Managed sandbox reaper sweep failed; retrying later")
            await asyncio.sleep(self._sandbox_config.reaper.sweep_interval_s)

    async def _sweep_current_workspace(self, workspace_id: int, now: int) -> int:
        cutoff = now - self._sandbox_config.reaper.terminate_after_offline_days * _SECONDS_PER_DAY
        hosts = await asyncio.to_thread(
            self._list_stale_managed_sandbox_hosts,
            workspace_id,
            cutoff,
        )
        grouped: dict[str, list[Host]] = {}
        for host in hosts:
            if not self._is_reap_candidate(host, cutoff=cutoff, now=now):
                continue
            assert host.sandbox_provider is not None
            grouped.setdefault(host.sandbox_provider, []).append(host)

        reaped = 0
        for provider, candidates in grouped.items():
            try:
                reaped += await asyncio.to_thread(
                    self._reap_group,
                    workspace_id,
                    provider,
                    candidates,
                    cutoff,
                    now,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "Managed sandbox reaper failed for provider %s in workspace %s; continuing",
                    provider,
                    workspace_id,
                )
        return reaped

    def _list_stale_managed_sandbox_hosts(
        self,
        workspace_id: int,
        cutoff: int,
    ) -> list[Host]:
        with workspace_scope(workspace_id):
            return self._host_store.list_stale_managed_sandbox_hosts(cutoff)

    def _reap_group(
        self,
        workspace_id: int,
        provider: str,
        candidates: list[Host],
        cutoff: int,
        now: int,
    ) -> int:
        with workspace_scope(workspace_id):
            launcher = _launcher_for_teardown(candidates[0], self._sandbox_config)
            if launcher is None:
                _logger.warning(
                    "No launcher available for managed sandbox provider %s; "
                    "skipping %s candidate(s)",
                    provider,
                    len(candidates),
                )
                return 0

            reaped = 0
            with launcher.reaper_identity(workspace_id):
                for candidate in candidates:
                    try:
                        sandbox_id = candidate.terminating_sandbox_id
                        if sandbox_id is None:
                            if not self._is_reap_candidate(candidate, cutoff=cutoff, now=now):
                                continue
                            sandbox_id = candidate.sandbox_id
                            assert sandbox_id is not None
                            if not self._host_store.detach_stale_managed_sandbox(
                                candidate.host_id,
                                sandbox_id=sandbox_id,
                                expected_updated_at=candidate.updated_at,
                            ):
                                continue
                        elif candidate.sandbox_id == sandbox_id:
                            _logger.error(
                                "Managed sandbox host %s has the same active and pending "
                                "sandbox id; skipping termination",
                                candidate.host_id,
                            )
                            continue
                        launcher.terminate(sandbox_id)
                        if self._host_store.mark_terminating_sandbox_terminated(
                            candidate.host_id,
                            sandbox_id=sandbox_id,
                        ):
                            reaped += 1
                    except Exception:
                        _logger.exception(
                            "Managed sandbox reaper failed for host %s; continuing",
                            candidate.host_id,
                        )
            return reaped

    @staticmethod
    def _is_reap_candidate(host: Host, *, cutoff: int, now: int) -> bool:
        return host.sandbox_provider is not None and (
            host.terminating_sandbox_id is not None
            or (
                host.sandbox_id is not None
                and not host_is_live(host, now=now)
                and host.updated_at <= cutoff
            )
        )
