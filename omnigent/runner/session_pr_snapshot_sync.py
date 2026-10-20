"""Fetch PR panel resources in the background with the runner's provider credentials."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from weakref import ReferenceType, WeakKeyDictionary, ref

import httpx
from cachetools import LRUCache

from omnigent.runner.session_prs import SessionPrRegistry, SessionPullRequest

_logger = logging.getLogger(__name__)
_RETRY_SECONDS = 15.0
_REFRESH_SECONDS = 300.0
_SnapshotKey = tuple[ReferenceType[httpx.AsyncClient], Path, str]


@dataclass(frozen=True)
class _SnapshotAttempt:
    activity: tuple[float, float]
    retry_at: float
    pending: dict[str, Any] | None = None


# A failed upload retains its bounded payload, while successful entries retain only a timestamp.
_attempts: LRUCache[_SnapshotKey, _SnapshotAttempt] = LRUCache(
    maxsize=16 * 1024 * 1024,
    getsizeof=lambda value: max(
        1024, len(json.dumps(value.pending, ensure_ascii=False).encode("utf-8"))
    ),
)
_tasks: dict[tuple[httpx.AsyncClient, Path], asyncio.Task[None]] = {}
_slots: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


def _activity(entry: SessionPullRequest) -> tuple[float, float]:
    return entry.first_seen_at, entry.last_seen_at


async def _workspace(
    session_id: str, client: httpx.AsyncClient, workspace: Path | None
) -> Path | None:
    if workspace is not None:
        return workspace if workspace.is_absolute() else None
    from omnigent.runner.native import _SESSION_METADATA_PARAMS

    response = await client.get(
        f"/v1/sessions/{session_id}", params=_SESSION_METADATA_PARAMS, timeout=5.0
    )
    response.raise_for_status()
    metadata = response.json()
    if not isinstance(metadata, dict) or metadata.get("id") != session_id:
        return None
    selected = metadata.get("workspace")
    if isinstance(selected, str) and selected.strip():
        root = Path(selected.strip()).expanduser()
    else:
        fallback = os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
        if not fallback:
            return None
        root = Path(fallback).expanduser()
        if os.environ.get("OMNIGENT_RUNNER_ISOLATE_SESSION") == "1":
            from omnigent.runner.resource_registry import _contained_session_dir

            root = Path(_contained_session_dir(root, session_id))
    return root if root.is_absolute() else None


def _read_resources(root: Path, entry: SessionPullRequest) -> dict[str, Any]:
    from omnigent.git_providers import load_facet
    from omnigent.stores.session_pr_snapshots import sanitize_pr_snapshot

    facet = load_facet(entry.provider, "pull_requests")
    if facet is None:
        return {}
    resources: dict[str, Any] = {}
    for kind, read in (
        ("info", facet.reference_info),
        ("changes", facet.changed_files),
        ("diff", facet.pr_diff),
    ):
        try:
            payload = read(str(root), entry)
            if kind == "info" and not isinstance(payload.get("pr"), dict):
                return {}
            if kind == "changes" and (payload.get("warning") or payload.get("has_more")):
                continue
            if kind == "diff" and payload.get("unavailable_reason") not in (
                None,
                "pr_outside_workspace",
            ):
                continue
            resources[kind] = sanitize_pr_snapshot(kind, payload, entry.url)
        except Exception:  # noqa: BLE001 — keep other successful resources and retry failures
            _logger.debug("Could not snapshot PR %s %s", entry.url, kind, exc_info=True)
            if kind == "info":
                return {}
    if "changes" in resources or "diff" in resources:
        try:
            latest = sanitize_pr_snapshot(
                "info", facet.reference_info(str(root), entry), entry.url
            )
            before = resources["info"].get("pr") or {}
            after = latest.get("pr") or {}
            if any(before.get(key) != after.get(key) for key in ("head_sha", "base_sha")):
                return {}
        except Exception:  # noqa: BLE001 — retry when the captured revision cannot be verified
            _logger.debug("Could not verify PR snapshot revision for %s", entry.url, exc_info=True)
            return {}
    return resources


async def _sync_snapshot(
    session_id: str,
    client: httpx.AsyncClient,
    registry: SessionPrRegistry,
    revision: int,
    entry: SessionPullRequest,
    root: Path,
) -> None:
    key = (ref(client), registry.path, entry.url)
    activity = _activity(entry)
    previous = _attempts.get(key)
    if previous is not None and previous.activity != activity:
        previous = None
    # Throttle failures too: ordinary tools and heartbeats share this path.
    _attempts[key] = _SnapshotAttempt(activity, time.monotonic() + _RETRY_SECONDS)
    pending = previous.pending if previous is not None else None
    if pending is None:
        captured_at_ns = max(time.time_ns(), revision)
        resources = await asyncio.to_thread(_read_resources, root, entry)
        if not resources:
            return
        pending = {"pr_url": entry.url, "captured_at_ns": captured_at_ns, **resources}
    current = await asyncio.to_thread(registry.snapshot)
    if current is None or not any(
        candidate.url == entry.url and _activity(candidate) == activity for candidate in current[1]
    ):
        _attempts.pop(key, None)
        return
    _attempts[key] = _SnapshotAttempt(activity, time.monotonic() + _RETRY_SECONDS, pending)
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_pr_snapshot", "data": pending},
        timeout=10.0,
    )
    response.raise_for_status()
    saved = response.json().get("saved", True)
    _attempts[key] = _SnapshotAttempt(
        activity,
        time.monotonic() + (_REFRESH_SECONDS if saved else _RETRY_SECONDS),
    )


def _due(
    client: httpx.AsyncClient, registry: SessionPrRegistry, entry: SessionPullRequest
) -> bool:
    previous = _attempts.get((ref(client), registry.path, entry.url))
    return (
        previous is None
        or previous.activity != _activity(entry)
        or previous.retry_at <= time.monotonic()
    )


async def _sync_snapshots(
    session_id: str,
    client: httpx.AsyncClient,
    registry: SessionPrRegistry,
    workspace: Path | None,
) -> None:
    try:
        snapshot = await asyncio.to_thread(registry.snapshot)
        if snapshot is None:
            return
        revision, entries = snapshot
        entries = [entry for entry in entries if _due(client, registry, entry)]
        if not entries:
            return
        root = await _workspace(session_id, client, workspace)
        if root is None:
            return
        slots = _slots.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(4))
        for entry in entries:
            # Keep payload references inside the slot so queued sessions cannot pin evicted data.
            async with slots:
                try:
                    await _sync_snapshot(session_id, client, registry, revision, entry, root)
                except Exception:  # noqa: BLE001 — retain this payload and continue other PRs
                    _logger.debug("Could not upload PR snapshot %s", entry.url, exc_info=True)
    except Exception:  # noqa: BLE001 — the next tool or reconnect retries optional snapshots
        _logger.debug("Could not sync PR snapshots for session %s", session_id, exc_info=True)


def schedule_session_pr_snapshots(
    session_id: str,
    client: httpx.AsyncClient,
    registry: SessionPrRegistry,
    *,
    workspace: Path | None = None,
) -> None:
    """Queue at most one enrichment pass per session without delaying tool completion."""
    key = (client, registry.path)
    if key in _tasks:
        return
    task = asyncio.create_task(_sync_snapshots(session_id, client, registry, workspace))
    _tasks[key] = task
    task.add_done_callback(lambda _done: _tasks.pop(key, None))
