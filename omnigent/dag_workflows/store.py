"""Store interface and in-memory implementation for workflow runs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from omnigent.dag_workflows.models import WorkflowRun


class WorkflowStore(Protocol):
    async def put(self, run: WorkflowRun) -> None: ...

    async def get(self, parent_session_id: str, workflow_id: str) -> WorkflowRun | None: ...

    async def list(self, parent_session_id: str) -> list[WorkflowRun]: ...

    async def mutate(
        self,
        parent_session_id: str,
        workflow_id: str,
        update: Callable[[WorkflowRun], None],
    ) -> WorkflowRun: ...

    async def remove_session(self, parent_session_id: str) -> None: ...


class InMemoryWorkflowStore:
    """Async-safe, JSON-serializable workflow state with no persistence claims."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], WorkflowRun] = {}
        self._lock = asyncio.Lock()

    async def put(self, run: WorkflowRun) -> None:
        async with self._lock:
            key = (run.parent_session_id, run.workflow_id)
            self._runs[key] = run.model_copy(deep=True)

    async def get(self, parent_session_id: str, workflow_id: str) -> WorkflowRun | None:
        async with self._lock:
            run = self._runs.get((parent_session_id, workflow_id))
            return run.model_copy(deep=True) if run is not None else None

    async def list(self, parent_session_id: str) -> list[WorkflowRun]:
        async with self._lock:
            return [
                run.model_copy(deep=True)
                for (session_id, _), run in self._runs.items()
                if session_id == parent_session_id
            ]

    async def mutate(
        self,
        parent_session_id: str,
        workflow_id: str,
        update: Callable[[WorkflowRun], None],
    ) -> WorkflowRun:
        async with self._lock:
            key = (parent_session_id, workflow_id)
            run = self._runs.get(key)
            if run is None:
                raise KeyError(workflow_id)
            update(run)
            self._runs[key] = run
            return run.model_copy(deep=True)

    async def remove_session(self, parent_session_id: str) -> None:
        async with self._lock:
            for key in [key for key in self._runs if key[0] == parent_session_id]:
                self._runs.pop(key, None)
