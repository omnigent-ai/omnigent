"""Runner-owned MCP executions that survive a server-tunnel replacement.

The Omnigent server reaches local MCP processes through a tunneled runner
request.  A server restart cancels that request, but it must not cancel and
then replay an external tool that may already have side effects.  This
registry shields the actual execution and lets the next server generation
reattach with the same operation id and step.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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

    def has_operation(self, session_id: str, operation_id: str) -> bool:
        """Return whether any step for an operation is retained."""
        self._prune()
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
                entry.completed_at = time.monotonic()

            task.add_done_callback(_mark_completed)

        # The surrounding ASGI dispatch belongs to one tunnel generation.
        # Its cancellation must not propagate into the external operation.
        return await asyncio.shield(entry.task)

    async def cancel_session(self, session_id: str) -> None:
        """Cancel and forget retained operations for a deleted session."""
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
        """Bound retention of completed results; never evict active work."""
        now = time.monotonic()
        completed: list[tuple[tuple[str, str, str], _Execution]] = []
        for key, entry in tuple(self._entries.items()):
            if entry.completed_at is None:
                continue
            if now - entry.completed_at >= _COMPLETED_TTL_S:
                self._entries.pop(key, None)
                continue
            completed.append((key, entry))
        if len(completed) <= _MAX_COMPLETED:
            return
        completed.sort(key=lambda item: item[1].completed_at or 0.0)
        for key, _entry in completed[: len(completed) - _MAX_COMPLETED]:
            self._entries.pop(key, None)
