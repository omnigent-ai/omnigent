"""Publish the durable local PR snapshot at tool completion and reconnect."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from weakref import WeakKeyDictionary, WeakValueDictionary

import httpx
from cachetools import LRUCache

from omnigent.runner.session_prs import SessionPrRegistry

_logger = logging.getLogger(__name__)
_sync_locks: WeakValueDictionary[tuple[httpx.AsyncClient, Path], asyncio.Lock] = (
    WeakValueDictionary()
)
_acknowledged: WeakKeyDictionary[httpx.AsyncClient, LRUCache[Path, int]] = WeakKeyDictionary()


async def restore_session_prs(
    session_id: str,
    entries: Sequence[dict[str, object]] | None,
    updated_at_ns: int | None = None,
) -> None:
    """Restore server associations without preventing session launch on a metadata error."""
    if entries is None:
        return
    try:
        await asyncio.to_thread(
            SessionPrRegistry(session_id).restore, entries, updated_at_ns=updated_at_ns
        )
    except Exception:  # noqa: BLE001 — optional metadata must not prevent a session from starting
        _logger.warning(
            "Failed to restore session PRs", extra={"session_id": session_id}, exc_info=True
        )


async def sync_session_prs(
    session_id: str,
    server_client: httpx.AsyncClient | None,
    *,
    workspace: Path | None = None,
) -> None:
    """Await server acknowledgement without letting metadata failures break a tool call."""
    if server_client is None:
        return
    registry = SessionPrRegistry(session_id)
    lock = _sync_locks.setdefault((server_client, registry.path), asyncio.Lock())
    async with lock:
        try:
            snapshot = await asyncio.to_thread(registry.snapshot)
            if snapshot is None:
                return
            updated_at_ns, entries = snapshot
            acknowledged = _acknowledged.setdefault(server_client, LRUCache(maxsize=1024))
            if acknowledged.get(registry.path) != updated_at_ns:
                response = await server_client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "external_session_prs",
                        "data": {
                            "prs": [entry.model_dump() for entry in entries],
                            "updated_at_ns": updated_at_ns,
                        },
                    },
                    timeout=5.0,
                )
                response.raise_for_status()
                acknowledged[registry.path] = updated_at_ns
            if entries:
                from omnigent.runner.session_pr_snapshot_sync import schedule_session_pr_snapshots

                schedule_session_pr_snapshots(
                    session_id, server_client, registry, workspace=workspace
                )
        except Exception:  # noqa: BLE001 — the local snapshot remains available for retry
            _logger.warning(
                "Failed to sync session PRs", extra={"session_id": session_id}, exc_info=True
            )
