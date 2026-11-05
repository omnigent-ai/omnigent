"""Single-process workflow hosting for the prototype UI, using local run checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from omnigent.workflows.engine import WorkflowRunner
from omnigent.workflows.execution import open_executor
from omnigent.workflows.gates import GateRequest
from omnigent.workflows.models import Workflow
from omnigent.workflows.simulation import simulate_gate
from omnigent.workflows.state import ExecutionBinding, GateOutcome
from omnigent.workflows.store import RunStore

logger = logging.getLogger(__name__)


async def reject_command_gate(_request: GateRequest) -> GateOutcome:
    raise ValueError("Web workflows require external gates, including dynamically added tasks")


class WorkflowService:
    """Scope files to the authenticated owner and retain tasks until they finish."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.tasks: dict[Path, asyncio.Task[None]] = {}
        self.errors: dict[Path, str] = {}

    def owner_directory(self, owner: str) -> Path:
        return self.directory / hashlib.sha256(owner.encode()).hexdigest()

    def store(self, owner: str, run_id: str) -> RunStore:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid workflow run ID")
        directory = self.owner_directory(owner) / run_id
        if not (directory / "state.json").is_file():
            raise FileNotFoundError("Workflow not found")
        return RunStore(directory)

    def inspect(self, store: RunStore) -> dict[str, Any]:
        task = self.tasks.get(store.path)
        return {
            **store.load(),
            "active": task is not None and not task.done(),
            "controller_error": self.errors.get(store.path),
        }

    def list_runs(self, owner: str) -> list[dict[str, Any]]:
        directory = self.owner_directory(owner)
        runs = []
        for path in sorted(
            directory.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:100]:
            saved = self.inspect(RunStore(path.parent))
            runs.append(
                {
                    "run_id": saved["run_id"],
                    "workflow_id": saved.get("workflow_id"),
                    "root_session_id": saved.get("root_session_id"),
                    "name": saved["workflow"]["name"],
                    "status": saved["status"],
                    "backend": saved["binding"]["mode"],
                    "active": saved["active"],
                }
            )
        return runs

    def create(
        self,
        owner: str,
        workflow: Workflow,
        binding: ExecutionBinding,
        *,
        workflow_id: str | None = None,
        root_session_id: str | None = None,
    ) -> RunStore:
        async def unused(_request: Any) -> str:
            raise RuntimeError("Executor is selected when the controller starts")

        runner = WorkflowRunner(
            workflow,
            unused,
            binding=binding,
            workflow_id=workflow_id,
            root_session_id=root_session_id,
        )
        store = RunStore(self.owner_directory(owner) / runner.run_id)
        store.save(runner.snapshot())
        self.start(store)
        return store

    def start(self, store: RunStore) -> None:
        task = self.tasks.get(store.path)
        if task is not None and not task.done():
            raise ValueError("This workflow already has an active controller")
        if store.load()["status"] in {"succeeded", "failed"}:
            raise ValueError("This workflow is terminal; start a new run")
        with store.controller_lock:
            pass
        self.errors.pop(store.path, None)
        self.tasks[store.path] = asyncio.create_task(self._drive(store))

    async def _drive(self, store: RunStore) -> None:
        try:
            snapshot = store.load()
            binding = ExecutionBinding.model_validate(snapshot["binding"])
            async with open_executor(binding) as execute:
                runner = WorkflowRunner.restore(
                    snapshot,
                    execute,
                    store=store,
                    validate=simulate_gate
                    if binding.mode == "simulation"
                    else reject_command_gate,
                )
                await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- expose background controller failures to its owner.
            self.errors[store.path] = str(exc)
            logger.warning("Workflow controller stopped: %s", type(exc).__name__)
        finally:
            self.tasks.pop(store.path, None)

    async def stop(self, store: RunStore) -> None:
        task = self.tasks.get(store.path)
        if task is None or task.done():
            raise ValueError("No controller is active on this server")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.tasks.pop(store.path, None)

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
