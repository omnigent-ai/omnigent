from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx

_logger = logging.getLogger(__name__)

NATIVE_SUBAGENT_SNAPSHOT_EVENT = "external_native_subagent_snapshot"
NATIVE_SUBAGENT_ACTIVE_STATUSES = frozenset({"running", "waiting", "activity_unverified"})
NATIVE_SUBAGENT_STATUSES = NATIVE_SUBAGENT_ACTIVE_STATUSES | frozenset(
    {"idle", "completed", "failed", "stopped", "killed"}
)
MAX_NATIVE_SNAPSHOT_CHILDREN = 512


@dataclass(frozen=True)
class NativeSubagentSnapshot:
    """A source-fenced inventory whose omissions never prove task outcomes."""

    generation: int
    sequence: int
    children: dict[str, str]
    complete: bool = True
    retired: bool = False

    @property
    def active_child_ids(self) -> frozenset[str]:
        return frozenset(
            child
            for child, status in self.children.items()
            if status in NATIVE_SUBAGENT_ACTIVE_STATUSES
        )


def parse_native_subagent_snapshot(payload: object) -> NativeSubagentSnapshot:
    """Reject malformed/oversized inventories instead of silently truncating."""
    if not isinstance(payload, Mapping):
        raise ValueError("snapshot data must be an object")
    generation, sequence = payload.get("generation"), payload.get("sequence")
    for name, value in (("generation", generation), ("sequence", sequence)):
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError(f"snapshot {name} must be a positive 63-bit integer")
    complete, retired = payload.get("complete", True), payload.get("retired", False)
    if not isinstance(complete, bool) or not isinstance(retired, bool):
        raise ValueError("snapshot complete/retired must be booleans")
    raw = payload.get("children")
    if not isinstance(raw, list) or len(raw) > MAX_NATIVE_SNAPSHOT_CHILDREN:
        raise ValueError("snapshot children must be a list of at most 512 entries")
    children: dict[str, str] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("snapshot child must be an object")
        child, status = row.get("session_id"), row.get("status")
        if not isinstance(child, str) or not child or len(child) > 512:
            raise ValueError("invalid snapshot child session_id")
        if child in children:
            raise ValueError("duplicate snapshot child session_id")
        if not isinstance(status, str) or status not in NATIVE_SUBAGENT_STATUSES:
            raise ValueError("invalid snapshot child status")
        children[child] = status
    if retired and children:
        raise ValueError("retired snapshot must have no children")
    assert isinstance(generation, int) and isinstance(sequence, int)
    return NativeSubagentSnapshot(generation, sequence, children, complete, retired)


@dataclass
class _PendingInventory:
    children: tuple[tuple[str, str], ...]
    complete: bool
    retired: bool
    changed_at: float
    sent: bool = False
    next_attempt: float = 0.0


class NativeSubagentSnapshotPublisher:
    """Bounded heartbeat/retry sender; unsupported old Servers disable it."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        heartbeat_s: float = 15.0,
        retry_s: float = 5.0,
    ) -> None:
        self._client = client
        self._heartbeat_s = heartbeat_s
        self._retry_s = retry_s
        self._generation = time.time_ns()
        self._sequence = 0
        self._inventories: dict[str, _PendingInventory] = {}
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._disabled = False

    async def __aenter__(self) -> NativeSubagentSnapshotPublisher:
        self._task = asyncio.create_task(self._run(), name="native-child-snapshots")
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - optional telemetry cannot break cleanup.
            _logger.warning("Native child snapshot publisher failed", exc_info=True)

    def update(
        self,
        parent_id: str,
        children: Mapping[str, str],
        *,
        retired: bool = False,
    ) -> None:
        if self._disabled:
            return
        if retired:
            children = {}
        complete = len(children) <= MAX_NATIVE_SNAPSHOT_CHILDREN
        rows = tuple(sorted(children.items()))[:MAX_NATIVE_SNAPSHOT_CHILDREN]
        now = time.monotonic()
        old = self._inventories.get(parent_id)
        if old is None and not rows and not retired:
            return
        if old and (old.children, old.complete, old.retired) == (rows, complete, retired):
            old.changed_at = now
            return
        if old is None and len(self._inventories) >= 64:
            if retired:
                return
            victim = next(
                (key for key, pending in self._inventories.items() if pending.retired),
                next(iter(self._inventories)),
            )
            self._inventories.pop(victim)
        self._inventories[parent_id] = _PendingInventory(
            rows,
            complete,
            retired,
            now,
            next_attempt=old.next_attempt if old is not None and not old.sent else 0.0,
        )
        self._changed.set()

    async def _run(self) -> None:
        while not self._disabled:
            self._changed.clear()
            next_due = float("inf")
            for parent_id, pending in list(self._inventories.items()):
                now = time.monotonic()
                active = any(s in NATIVE_SUBAGENT_ACTIVE_STATUSES for _, s in pending.children)
                if pending.sent and not active:
                    continue
                if pending.next_attempt > now:
                    next_due = min(next_due, pending.next_attempt)
                    continue
                self._sequence += 1
                status, terminal = 0, False
                try:
                    response = await self._client.post(
                        f"/v1/sessions/{quote(parent_id, safe='')}/events",
                        json={
                            "type": NATIVE_SUBAGENT_SNAPSHOT_EVENT,
                            "data": {
                                "generation": self._generation,
                                "sequence": self._sequence,
                                "children": [
                                    {"session_id": child, "status": status}
                                    for child, status in pending.children
                                ],
                                "complete": pending.complete,
                                "retired": pending.retired,
                            },
                        },
                        timeout=10.0,
                    )
                    err: object = None
                    if response.status_code == 400:
                        with contextlib.suppress(ValueError):
                            body = response.json()
                            if isinstance(body, Mapping):
                                err = body.get("error")
                    unsupported = isinstance(err, Mapping) and str(err.get("message")).startswith(
                        "Unknown event type:"
                    )
                    if unsupported:
                        self._disabled = True
                        return
                    status = response.status_code
                    accepted = 200 <= status < 300 or (status == 404 and pending.retired)
                    terminal = status in {400, 401, 403, 404, 405, 413, 415, 422}
                    if status == 404 and pending.retired:
                        _logger.info("Native child snapshot parent already retired: %s", parent_id)
                except (httpx.HTTPError, ConnectionError):
                    accepted = False
                if terminal:
                    _logger.warning(
                        "Native child snapshot rejected parent %s with HTTP %s",
                        parent_id,
                        status,
                    )
                    self._inventories.pop(parent_id, None)
                    continue
                current = self._inventories.get(parent_id)
                if current is not pending:
                    if current is not None and not accepted:
                        current.next_attempt = max(
                            current.next_attempt,
                            time.monotonic() + self._retry_s,
                        )
                    continue
                if accepted:
                    pending.sent = True
                    if pending.retired:
                        self._inventories.pop(parent_id, None)
                        continue
                pending.next_attempt = time.monotonic() + (
                    self._heartbeat_s if accepted else self._retry_s
                )
                next_due = min(next_due, pending.next_attempt)
            delay = max(0.0, next_due - time.monotonic())
            if delay == float("inf"):
                await self._changed.wait()
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=delay)
