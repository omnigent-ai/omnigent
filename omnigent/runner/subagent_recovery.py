"""Runner-side recovery of sub-agent work after a parent session's inbox is recreated."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from omnigent.runner.app import _SessionSnapshot

import httpx
from fastapi import FastAPI

from omnigent.runner.subagent_work import (
    _SUBAGENT_TERMINAL_STATUSES,
    _deliver_subagent_completion,
    _drained_delivered_subagent_children,
    _recover_subagent_results_from_server,
    _subagent_recovery_done,
    _subagent_recovery_locks,
    _subagent_work_by_child,
    _SubagentRecoveryReadError,
    _SubagentWorkEntry,
    get_subagent_work,
    list_subagent_work,
    register_subagent_work,
)
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger("omnigent.runner.app")


class _ScheduleSubagentWakeFn(Protocol):
    def __call__(self, entry: _SubagentWorkEntry, *, is_rewake: bool = False) -> None: ...


@dataclasses.dataclass(frozen=True)
class SubagentRecovery:
    """Sub-agent recovery helpers the rest of the runner app calls."""

    cancel_subagent_recovery: Callable[[str], Coroutine[Any, Any, None]]
    deliver_retained_subagent_results: Callable[[str], None]
    ensure_subagent_work_entry: Callable[[str], Coroutine[Any, Any, _SubagentWorkEntry | None]]
    parent_is_nested_subagent: Callable[[_SubagentWorkEntry], Coroutine[Any, Any, bool]]
    recover_sub_agent_name: Callable[[str], Coroutine[Any, Any, str | None]]
    recover_undrained_subagent_results: Callable[[str], Coroutine[Any, Any, None]]
    start_subagent_recovery: Callable[[str], asyncio.Task[None]]


def build_subagent_recovery(
    app: FastAPI,
    *,
    _background_tasks: set[asyncio.Task[Any]],
    _schedule_subagent_wake: _ScheduleSubagentWakeFn,
    _session_inboxes: dict[str, asyncio.Queue[_JsonObject]],
    _session_snapshot: Callable[[str], Coroutine[Any, Any, _SessionSnapshot]],
    _session_sub_agent_names: dict[str, str],
    _subagent_recovery_tasks: dict[str, asyncio.Task[None]],
    server_client: httpx.AsyncClient,
) -> SubagentRecovery:
    """Build the sub-agent recovery helpers over the runner app's session state.

    The keyword arguments are the runner app's shared session state and helpers.
    """

    async def _recover_sub_agent_name(conv_id: str) -> str | None:
        cached = _session_sub_agent_names.get(conv_id)
        if cached:
            return cached
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        name = snapshot.sub_agent_name if snapshot is not None else None
        if name:
            _session_sub_agent_names[conv_id] = name
        return name

    async def _ensure_subagent_work_entry(conv_id: str) -> _SubagentWorkEntry | None:
        existing = get_subagent_work(conv_id)
        if existing is not None:
            return existing
        if conv_id in _drained_delivered_subagent_children:
            return None
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        parent_id = snapshot.parent_session_id
        if not parent_id or parent_id == conv_id:
            return None
        agent = snapshot.sub_agent_name or snapshot.agent_name or "sub-agent"
        return register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=conv_id,
            agent=agent,
            title=snapshot.sub_agent_name or "",
        )

    async def _parent_is_nested_subagent(entry: _SubagentWorkEntry) -> bool:
        """
        Return whether an undelivered result's parent is itself a sub-agent.

        A mirrored claude-native sub-agent never runs on this runner, so its
        inbox never exists here and retrying its children's terminal status
        every 30 s buys nothing. The inbox record is redundant for that
        topology: the child's result reaches the parent natively inside the
        Claude process. The status is acknowledged and the entry kept, so a
        sub-agent parent that does run here later still receives it when its
        inbox is created (``_deliver_retained_subagent_results``). An unreadable
        parent snapshot reads as a top-level parent, so the retry contract still
        covers a parent that lives elsewhere or is re-initializing after a
        restart.

        :param entry: Terminal work entry whose parent inbox was missing.
        :returns: ``True`` when the parent's snapshot names its own parent.
        """
        snapshot = await _session_snapshot(entry.parent_session_id)
        return snapshot.ok and snapshot.parent_session_id is not None

    def _deliver_retained_subagent_results(parent_id: str) -> None:
        """
        Hand over results acknowledged while ``parent_id`` had no inbox here.

        A terminal child whose sub-agent parent had no inbox here is
        acknowledged with its entry kept undelivered. If that parent later
        runs on this runner, creating its inbox delivers those entries and
        wakes it, as the forwarder's pending retry used to the moment the inbox
        appeared. A parent that never runs here keeps the entry undelivered;
        for a claude-native mirror the result already reached it natively.
        Idempotent: delivered entries are skipped.

        :param parent_id: Parent whose inbox now exists, e.g. ``"conv_parent123"``.
        :returns: None.
        """
        for entry in list_subagent_work(parent_id):
            if entry.status not in _SUBAGENT_TERMINAL_STATUSES or entry.delivered:
                continue
            if _deliver_subagent_completion(entry).delivered_now:
                _schedule_subagent_wake(entry)

    async def _run_subagent_recovery(parent_id: str) -> None:
        """
        Re-queue terminal child results lost with a runner process restart.

        The parent inbox is a process-local queue, so a result queued before
        a restart but not yet drained would otherwise vanish. Runs once per
        parent per process; pending recovered work is refreshed by the periodic
        sweep. A failed server read is retried before the next ``sys_read_inbox``
        drain. The inbox is created here when missing: after a reconnect the
        server can dispatch a pending message before it re-initializes the session,
        and that turn's drain
        must still see the recovered results. Results acknowledged while this
        parent had no inbox here are handed over first, on every call.

        :param parent_id: Parent session whose inbox was recreated, e.g.
            ``"conv_parent123"``.
        :returns: None.
        """
        _session_inboxes.setdefault(parent_id, asyncio.Queue())
        _deliver_retained_subagent_results(parent_id)
        if parent_id in _subagent_recovery_done:
            return
        lock = _subagent_recovery_locks.setdefault(parent_id, asyncio.Lock())
        async with lock:
            if parent_id in _subagent_recovery_done:
                return
            try:
                await _recover_subagent_results_from_server(
                    server_client=server_client,
                    parent_id=parent_id,
                    schedule_wake=_schedule_subagent_wake,
                )
            except (httpx.HTTPError, _SubagentRecoveryReadError, ValueError):
                _logger.warning(
                    "Failed to recover undrained sub-agent results for %s",
                    parent_id,
                    exc_info=True,
                    extra={"session_id": parent_id},
                )
                return
            _subagent_recovery_done.add(parent_id)

    def _start_subagent_recovery(parent_id: str) -> asyncio.Task[None]:
        """Return the session-owned single-flight restart recovery task."""
        task = _subagent_recovery_tasks.get(parent_id)
        if task is not None and not task.done():
            return task
        _subagent_recovery_tasks.pop(parent_id, None)
        task = asyncio.create_task(
            _run_subagent_recovery(parent_id),
            name=f"subagent-recovery:{parent_id}",
        )
        _subagent_recovery_tasks[parent_id] = task
        _background_tasks.add(task)

        def _drop_completed_recovery(done: asyncio.Task[None]) -> None:
            _background_tasks.discard(done)
            if _subagent_recovery_tasks.get(parent_id) is done:
                _subagent_recovery_tasks.pop(parent_id, None)

        task.add_done_callback(_drop_completed_recovery)
        return task

    async def _cancel_subagent_recovery(parent_id: str) -> None:
        """Stop recovery before deleting its session-local inbox and markers."""
        task = _subagent_recovery_tasks.pop(parent_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - recovery failure must not block session deletion
            _logger.warning(
                "Sub-agent recovery failed while deleting session %s",
                parent_id,
                exc_info=True,
                extra={"session_id": parent_id},
            )

    async def _recover_undrained_subagent_results(parent_id: str) -> None:
        """Await the session-owned single-flight restart recovery task."""
        await asyncio.shield(_start_subagent_recovery(parent_id))

    app.state.recover_undrained_subagent_results = _recover_undrained_subagent_results

    async def _reconcile_pending_subagent_results() -> None:
        """Refresh only recovered work with no local execution or completion edge."""
        parents = {
            entry.parent_session_id
            for entry in list(_subagent_work_by_child.values())
            if entry.status == "waiting"
        }
        for parent_id in parents:
            if not any(entry.status == "waiting" for entry in list_subagent_work(parent_id)):
                continue
            _subagent_recovery_done.discard(parent_id)
            await _recover_undrained_subagent_results(parent_id)

    app.state.reconcile_pending_subagent_results = _reconcile_pending_subagent_results

    return SubagentRecovery(
        cancel_subagent_recovery=_cancel_subagent_recovery,
        deliver_retained_subagent_results=_deliver_retained_subagent_results,
        ensure_subagent_work_entry=_ensure_subagent_work_entry,
        parent_is_nested_subagent=_parent_is_nested_subagent,
        recover_sub_agent_name=_recover_sub_agent_name,
        recover_undrained_subagent_results=_recover_undrained_subagent_results,
        start_subagent_recovery=_start_subagent_recovery,
    )
