"""Runner-owned MCP executions that survive a server-tunnel replacement.

The Omnigent server reaches local MCP processes through a tunneled runner
request.  A server restart cancels that request, but it must not cancel and
then replay an external tool that may already have side effects.  This
registry shields the actual execution and lets the next server generation
reattach with the same operation id and step. The originating proxy reserves
the operation until its call ends, so completed steps cannot expire mid-retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from omnigent.json_types import JsonObject

_COMPLETED_TTL_S = 300.0
_MAX_COMPLETED = 1024

# Private runner/server protocol fields. The detached error is only actionable
# when the caller can prove it still owns the matching operation locally.
MCP_OPERATION_ID_PARAM = "_omnigent_operation_id"
RUNNER_MCP_EXECUTION_DETACHED_CODE = -32098
RUNNER_MCP_EXECUTION_DETACHED_MESSAGE = "Runner MCP execution detached."


class McpExecutionConflict(RuntimeError):
    """An operation id was retried with different execution parameters."""


@dataclass(frozen=True)
class McpExecutionResult:
    """Serializable response produced by one runner MCP execution."""

    status_code: int
    content: JsonObject


@dataclass
class _Execution:
    """One in-flight or recently completed operation step."""

    fingerprint: str
    task: asyncio.Task[McpExecutionResult]
    completed_at: float | None = None


def _fingerprint(params: JsonObject) -> str:
    """Return a stable digest for JSON request parameters."""
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class McpExecutionRegistry:
    """Deduplicate runner MCP work across tunneled request lifetimes."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], _Execution] = {}
        self._operation_leases: dict[tuple[str, str], int] = {}
        self._released_at: dict[tuple[str, str], float] = {}

    def retain_operation(self, session_id: str, operation_id: str) -> None:
        """Keep an operation reattachable while its originating call is live."""
        key = (session_id, operation_id)
        self._operation_leases[key] = self._operation_leases.get(key, 0) + 1
        self._released_at.pop(key, None)
        self._prune()

    def release_operation(self, session_id: str, operation_id: str) -> None:
        """Release one live caller's retention lease."""
        key = (session_id, operation_id)
        leases = self._operation_leases.get(key, 0)
        if leases > 1:
            self._operation_leases[key] = leases - 1
            return
        if leases == 0:
            return
        self._operation_leases.pop(key, None)
        if any(entry_key[:2] == key for entry_key in self._entries):
            self._released_at[key] = monotonic()
        self._prune()

    def has_operation(self, session_id: str, operation_id: str) -> bool:
        """Return whether an operation is reserved or has a retained step."""
        self._prune()
        if (session_id, operation_id) in self._operation_leases:
            return True
        return any(
            stored_session == session_id and stored_operation == operation_id
            for stored_session, stored_operation, _step in self._entries
        )

    async def execute(
        self,
        *,
        session_id: str,
        operation_id: str,
        step: str,
        params: JsonObject,
        run: Callable[[], Awaitable[McpExecutionResult]],
    ) -> McpExecutionResult:
        """Run one logical step once, or attach to its retained task/result."""
        self._prune()
        key = (session_id, operation_id, step)
        fingerprint = _fingerprint(params)
        entry = self._entries.get(key)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                raise McpExecutionConflict(
                    "MCP operation parameters changed while reconnecting; "
                    "refusing to execute the external tool again"
                )
        else:

            async def _run() -> McpExecutionResult:
                return await run()

            task = asyncio.create_task(
                _run(),
                name=f"mcp-execution:{session_id}:{operation_id}:{step}",
            )
            entry = _Execution(fingerprint=fingerprint, task=task)
            self._entries[key] = entry

            def _mark_completed(_task: asyncio.Task[McpExecutionResult]) -> None:
                entry.completed_at = monotonic()

            task.add_done_callback(_mark_completed)

        # The surrounding ASGI dispatch belongs to one tunnel generation.
        # Its cancellation must not propagate into the external operation.
        return await asyncio.shield(entry.task)

    async def cancel_session(self, session_id: str) -> None:
        """Cancel and forget retained operations for a deleted session."""
        for key in tuple(self._operation_leases):
            if key[0] == session_id:
                self._operation_leases.pop(key, None)
        for key in tuple(self._released_at):
            if key[0] == session_id:
                self._released_at.pop(key, None)

        tasks: list[asyncio.Task[McpExecutionResult]] = []
        for key, entry in tuple(self._entries.items()):
            if key[0] != session_id:
                continue
            self._entries.pop(key, None)
            if not entry.task.done():
                entry.task.cancel()
                tasks.append(entry.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _prune(self) -> None:
        """Bound unleased completed results; never evict live caller work."""
        now = monotonic()
        completed: list[tuple[tuple[str, str, str], _Execution, float]] = []
        for key, entry in tuple(self._entries.items()):
            if entry.completed_at is None:
                continue
            operation_key = key[:2]
            if operation_key in self._operation_leases:
                continue
            retained_at = max(
                entry.completed_at,
                self._released_at.get(operation_key, entry.completed_at),
            )
            if now - retained_at >= _COMPLETED_TTL_S:
                self._entries.pop(key, None)
                continue
            completed.append((key, entry, retained_at))
        if len(completed) > _MAX_COMPLETED:
            completed.sort(key=lambda item: item[2])
            for key, _entry, _retained_at in completed[: len(completed) - _MAX_COMPLETED]:
                self._entries.pop(key, None)

        retained_operations = {key[:2] for key in self._entries}
        for key in tuple(self._released_at):
            if key not in retained_operations:
                self._released_at.pop(key, None)
