"""
Orphan reaper for the ``docker`` managed-sandbox provider.

The server provisions one container per managed session and removes it on
session teardown. A server crash/restart can leave containers with no
backing host row; this reaper removes those. It runs in-process (the
server already owns host lifecycle) on startup and on a periodic sweep,
and is started from the FastAPI lifespan only when the configured sandbox
provider is ``docker``.

A container is reaped only when ALL hold:

- it carries the managed + docker labels (set at provision), and
- its id is NOT in ``HostStore.list_managed_sandbox_ids("docker")`` — no
  host row references it, online OR offline, and
- it is older than the grace period, so a just-provisioned container is
  never removed before its host finishes registering.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)

DEFAULT_REAPER_INTERVAL_S = 300
DEFAULT_REAPER_GRACE_S = 300

_MANAGED_LABEL_FILTER = ["omnigent.managed=1", "omnigent.provider=docker"]


def _container_created_at(container: Any) -> int:
    """Parse the ``omnigent.created_at`` label; missing/garbage → epoch 0."""
    labels = getattr(container, "labels", {}) or {}
    raw = labels.get("omnigent.created_at")
    if not isinstance(raw, str):
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def reap_docker_sandboxes_once(
    *,
    client: Any,
    host_store: HostStore,
    grace_s: int,
) -> list[str]:
    """
    Remove orphaned managed docker containers once. Returns the removed ids.

    :param client: A Docker SDK client (``docker.from_env()``).
    :param host_store: The server's host store, queried for backed ids.
    :param grace_s: Containers younger than this (by ``created_at`` label)
        are skipped, so a just-provisioned container is never reaped before
        its host registers.
    """
    backed = host_store.list_managed_sandbox_ids("docker")
    now = int(time.time())
    removed: list[str] = []
    containers = client.containers.list(all=True, filters={"label": _MANAGED_LABEL_FILTER})
    for container in containers:
        container_id = getattr(container, "id", "")
        if not container_id or container_id in backed:
            continue
        if now - _container_created_at(container) < grace_s:
            continue
        try:
            container.remove(force=True)
        except Exception:  # noqa: BLE001 - best-effort reap; log and continue
            _logger.warning(
                "sandbox reaper: failed to remove orphaned container %s",
                container_id,
                exc_info=True,
            )
            continue
        removed.append(container_id)
    if removed:
        _logger.info("sandbox reaper removed %d orphaned container(s)", len(removed))
    return removed


async def run_docker_sandbox_reaper(
    *,
    client: Any,
    host_store: HostStore,
    interval_s: int = DEFAULT_REAPER_INTERVAL_S,
    grace_s: int = DEFAULT_REAPER_GRACE_S,
) -> None:
    """Reap on a loop until cancelled (the caller does the startup reap)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(
                reap_docker_sandboxes_once,
                client=client,
                host_store=host_store,
                grace_s=grace_s,
            )
        except Exception as exc:  # noqa: BLE001 - a sweep error must not kill the loop
            _logger.warning("sandbox reaper sweep failed: %s", exc)


async def cancel_reaper_task(task: asyncio.Task[None] | None) -> None:
    """Cancel and await the reaper task. Idempotent; ``None`` is a no-op."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
