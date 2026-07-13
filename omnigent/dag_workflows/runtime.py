"""Event-driven controller for in-memory workflow DAG runs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jsonschema

from omnigent.dag_workflows.models import (
    NodeAttempt,
    NodeState,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowRuntimeConfig,
    WorkflowStatus,
    validate_definition_limits,
)
from omnigent.dag_workflows.store import InMemoryWorkflowStore, WorkflowStore

_logger = logging.getLogger(__name__)

DispatchCallback = Callable[
    [WorkflowNode, str, dict[str, Any], str | None], Awaitable[dict[str, Any]]
]
CancelCallback = Callable[[str], Awaitable[None]]
WakeCallback = Callable[[str], Awaitable[None]]
PublishCallback = Callable[[dict[str, Any]], None]
UsageCallback = Callable[[str], Awaitable[float]]
ResultPolicyCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_RESULT_RE = re.compile(
    r"<workflow_result>\s*(\{.*?\})\s*</workflow_result>",
    flags=re.DOTALL,
)
_NON_RETRYABLE_MARKERS = (
    "requires the '",
    " cli on path",
    "sub-agent type",
    "not found in agent spec",
    "invalid 'model'",
    "not supported for sub-agent",
    "denied by policy",
    "authorization",
)


@dataclass(frozen=True)
class WorkflowRef:
    workflow_id: str
    node_id: str
    attempt: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "node_id": self.node_id,
            "attempt": self.attempt,
        }


class WorkflowManager:
    """Own workflow state and reconciliation for one parent session."""

    def __init__(
        self,
        *,
        parent_session_id: str,
        limits: WorkflowRuntimeConfig,
        dispatch: DispatchCallback,
        cancel_child: CancelCallback,
        wake_parent: WakeCallback,
        publish: PublishCallback,
        get_child_cost: UsageCallback,
        evaluate_result: ResultPolicyCallback,
        store: WorkflowStore | None = None,
    ) -> None:
        self.parent_session_id = parent_session_id
        self.limits = limits
        self._dispatch = dispatch
        self._cancel_child = cancel_child
        self._wake_parent = wake_parent
        self._publish = publish
        self._get_child_cost = get_child_cost
        self._evaluate_result = evaluate_result
        self._store = store or InMemoryWorkflowStore()
        self._reconcile_locks: dict[str, asyncio.Lock] = {}
        self._reconcile_tasks: dict[str, asyncio.Task[None]] = {}
        self._completion_queues: dict[str, asyncio.Queue[tuple[WorkflowRef, dict[str, Any]]]] = {}

    async def submit(self, raw_definition: dict[str, Any]) -> WorkflowRun:
        definition = WorkflowDefinition.model_validate(raw_definition)
        validate_definition_limits(definition, self.limits)
        existing = await self._store.get(self.parent_session_id, definition.id)
        if existing is not None:
            raise ValueError(f"workflow {definition.id!r} already exists")
        run = WorkflowRun.from_definition(self.parent_session_id, definition)
        await self._store.put(run)
        self._completion_queues[run.workflow_id] = asyncio.Queue()
        self._emit(run)
        return run

    async def start(self, workflow_id: str, version: int, definition_hash: str) -> WorkflowRun:
        def update(run: WorkflowRun) -> None:
            if run.definition.version != version:
                raise ValueError(
                    "workflow version mismatch: "
                    f"current={run.definition.version}, requested={version}"
                )
            if run.definition_hash != definition_hash:
                raise ValueError("workflow definition_hash does not match the current definition")
            if run.status in (
                WorkflowStatus.SUCCEEDED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
            ):
                raise ValueError(f"workflow is already terminal: {run.status.value}")
            if any(node.state == NodeState.BLOCKED for node in run.nodes.values()):
                raise ValueError("workflow has blocked nodes; amend or remove them before restart")
            run.status = WorkflowStatus.RUNNING
            run.blocked_reason = None
            run.terminal_wake_sent = False
            run.updated_at = time.time()

        run = await self._mutate(workflow_id, update)
        self._completion_queues.setdefault(workflow_id, asyncio.Queue())
        self._emit(run)
        self._schedule_reconcile(workflow_id)
        return run

    async def amend(
        self,
        workflow_id: str,
        expected_version: int,
        delta: dict[str, Any],
    ) -> WorkflowRun:
        current = await self.require(workflow_id)
        if current.definition.version != expected_version:
            raise ValueError(
                f"workflow version mismatch: current={current.definition.version}, "
                f"expected={expected_version}"
            )
        nodes = {node.id: node for node in current.definition.nodes}
        mutable = {NodeState.PENDING, NodeState.READY, NodeState.BLOCKED}
        replaced_ids: set[str] = set()
        for node_id in delta.get("remove_node_ids", []):
            node_run = current.nodes.get(node_id)
            if node_run is None:
                raise ValueError(f"cannot remove unknown node {node_id!r}")
            if node_run.state not in mutable:
                raise ValueError(f"cannot remove {node_id!r} in state {node_run.state.value}")
            nodes.pop(node_id)
        for raw_node in delta.get("replace_nodes", []):
            node = WorkflowNode.model_validate(raw_node)
            node_run = current.nodes.get(node.id)
            if node_run is None:
                raise ValueError(f"cannot replace unknown node {node.id!r}")
            if node_run.state not in mutable:
                raise ValueError(f"cannot replace {node.id!r} in state {node_run.state.value}")
            nodes[node.id] = node
            replaced_ids.add(node.id)
        for raw_node in delta.get("add_nodes", []):
            node = WorkflowNode.model_validate(raw_node)
            if node.id in nodes:
                raise ValueError(f"cannot add duplicate node {node.id!r}")
            nodes[node.id] = node
        amended = WorkflowDefinition(
            id=current.definition.id,
            name=str(delta.get("name", current.definition.name)),
            version=current.definition.version + 1,
            budget=delta.get("budget", current.definition.budget.model_dump()),
            nodes=list(nodes.values()),
        )
        validate_definition_limits(amended, self.limits)

        def update(run: WorkflowRun) -> None:
            old_ids = set(run.nodes)
            new_ids = {node.id for node in amended.nodes}
            for removed in old_ids - new_ids:
                run.nodes.pop(removed, None)
            for node in amended.nodes:
                if node.id not in run.nodes or node.id in replaced_ids:
                    run.nodes[node.id] = WorkflowNodeRun(node_id=node.id)
            run.definition = amended
            run.definition_hash = amended.definition_hash
            run.status = WorkflowStatus.DRAFT
            run.blocked_reason = None
            run.terminal_wake_sent = False
            run.updated_at = time.time()

        run = await self._mutate(workflow_id, update)
        self._emit(run)
        return run

    async def cancel(self, workflow_id: str) -> WorkflowRun:
        run = await self.require(workflow_id)
        child_ids = [
            node.child_session_id
            for node in run.nodes.values()
            if node.state == NodeState.RUNNING and node.child_session_id is not None
        ]
        await asyncio.gather(
            *(self._cancel_child(child_id) for child_id in child_ids),
            return_exceptions=True,
        )

        def update(value: WorkflowRun) -> None:
            value.status = WorkflowStatus.CANCELLED
            value.blocked_reason = "cancelled by workflow owner"
            for node_run in value.nodes.values():
                if node_run.state not in (NodeState.SUCCEEDED, NodeState.FAILED):
                    node_run.state = NodeState.CANCELLED
                    node_run.reserved_cost_usd = 0.0
            value.reserved_cost_usd = 0.0
            value.updated_at = time.time()

        run = await self._mutate(workflow_id, update)
        self._emit(run)
        await self._wake_terminal(run)
        return run

    async def require(self, workflow_id: str) -> WorkflowRun:
        run = await self._store.get(self.parent_session_id, workflow_id)
        if run is None:
            raise KeyError(workflow_id)
        return run

    async def list(self) -> list[WorkflowRun]:
        return await self._store.list(self.parent_session_id)

    def enqueue_completion(self, ref: WorkflowRef, payload: dict[str, Any]) -> bool:
        queue = self._completion_queues.get(ref.workflow_id)
        if queue is None:
            return False
        queue.put_nowait((ref, payload))
        self._schedule_reconcile(ref.workflow_id)
        return True

    async def close(self) -> None:
        for task in self._reconcile_tasks.values():
            task.cancel()
        if self._reconcile_tasks:
            await asyncio.gather(*self._reconcile_tasks.values(), return_exceptions=True)
        await self._store.remove_session(self.parent_session_id)

    def _schedule_reconcile(self, workflow_id: str) -> None:
        task = self._reconcile_tasks.get(workflow_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._reconcile(workflow_id),
            name=f"workflow-reconcile-{workflow_id}",
        )
        self._reconcile_tasks[workflow_id] = task
        task.add_done_callback(lambda done: self._on_reconcile_done(workflow_id, done))

    def _on_reconcile_done(self, workflow_id: str, task: asyncio.Task[None]) -> None:
        if self._reconcile_tasks.get(workflow_id) is task:
            self._reconcile_tasks.pop(workflow_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.exception("workflow reconcile failed: workflow=%s", workflow_id, exc_info=exc)
            return
        queue = self._completion_queues.get(workflow_id)
        if queue is not None and not queue.empty():
            self._schedule_reconcile(workflow_id)

    async def _reconcile(self, workflow_id: str) -> None:
        lock = self._reconcile_locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:
            run = await self.require(workflow_id)
            queue = self._completion_queues.setdefault(workflow_id, asyncio.Queue())
            while not queue.empty():
                ref, payload = queue.get_nowait()
                run = await self._apply_completion(run, ref, payload)
            if run.status != WorkflowStatus.RUNNING:
                return
            run = await self._refresh_ready_states(run)
            running = sum(1 for node in run.nodes.values() if node.state == NodeState.RUNNING)
            free_slots = max(0, run.definition.budget.max_concurrency - running)
            ready = [
                node
                for node in run.definition.nodes
                if run.nodes[node.id].state == NodeState.READY
            ]
            ready.sort(key=lambda node: (-self._critical_path(run.definition, node.id), node.id))
            for node in ready[:free_slots]:
                latest = await self.require(workflow_id)
                if latest.status != WorkflowStatus.RUNNING:
                    break
                if latest.dispatch_count >= latest.definition.budget.max_dispatches:
                    run = await self._block(latest, "workflow dispatch budget exhausted")
                    break
                if not self._reserve_cost(latest, node):
                    run = await self._block(
                        latest, "workflow cost budget cannot reserve next node"
                    )
                    break
                await self._store.put(latest)
                run = await self._dispatch_node(latest, node)
            run = await self.require(workflow_id)
            await self._settle_workflow(run)

    async def _refresh_ready_states(self, run: WorkflowRun) -> WorkflowRun:
        by_id = {node.id: node for node in run.definition.nodes}

        def update(value: WorkflowRun) -> None:
            for node_id, node_run in value.nodes.items():
                if node_run.state not in (NodeState.PENDING, NodeState.READY):
                    continue
                deps = [value.nodes[dep].state for dep in by_id[node_id].deps]
                node_run.state = (
                    NodeState.READY
                    if all(state == NodeState.SUCCEEDED for state in deps)
                    else NodeState.PENDING
                )
            value.updated_at = time.time()

        updated = await self._mutate(run.workflow_id, update)
        self._emit(updated)
        return updated

    async def _dispatch_node(self, run: WorkflowRun, node: WorkflowNode) -> WorkflowRun:
        node_run = run.nodes[node.id]
        attempt_number = node_run.attempt_count + 1
        ref = WorkflowRef(run.workflow_id, node.id, attempt_number)
        prompt = self._node_prompt(run, node, attempt_number)
        existing_session = node_run.child_session_id
        try:
            handle = await self._dispatch(node, prompt, ref.as_dict(), existing_session)
        except Exception as exc:  # noqa: BLE001 - converted into workflow state
            return await self._dispatch_failed(run, node, attempt_number, str(exc))
        error = handle.get("error")
        if error:
            return await self._dispatch_failed(run, node, attempt_number, str(error))
        child_id = handle.get("conversation_id")
        if not isinstance(child_id, str) or not child_id:
            return await self._dispatch_failed(
                run, node, attempt_number, "sub-agent dispatch returned no conversation_id"
            )

        def update(value: WorkflowRun) -> None:
            current = value.nodes[node.id]
            current.state = NodeState.RUNNING
            current.child_session_id = child_id
            current.error = None
            current.attempts.append(
                NodeAttempt(number=attempt_number, child_session_id=child_id, status="running")
            )
            value.dispatch_count += 1
            value.updated_at = time.time()

        updated = await self._mutate(run.workflow_id, update)
        _logger.info(
            "workflow node dispatched workflow=%s node=%s attempt=%d child=%s",
            run.workflow_id,
            node.id,
            attempt_number,
            child_id,
        )
        self._emit(updated)
        return updated

    async def _dispatch_failed(
        self, run: WorkflowRun, node: WorkflowNode, attempt_number: int, error: str
    ) -> WorkflowRun:
        retryable = not self._non_retryable(error)

        def update(value: WorkflowRun) -> None:
            current = value.nodes[node.id]
            current.attempts.append(
                NodeAttempt(
                    number=attempt_number,
                    status="failed",
                    error=error,
                    completed_at=time.time(),
                )
            )
            self._release_cost(value, current)
            current.error = error
            if retryable and current.attempt_count < node.max_attempts:
                current.state = NodeState.PENDING
            else:
                current.state = NodeState.BLOCKED
                value.status = WorkflowStatus.BLOCKED
                value.blocked_reason = f"node {node.id} failed: {error}"
            value.updated_at = time.time()

        updated = await self._mutate(run.workflow_id, update)
        self._emit(updated)
        if updated.status == WorkflowStatus.BLOCKED:
            await self._wake_terminal(updated)
        else:
            self._schedule_reconcile(updated.workflow_id)
        return updated

    async def _apply_completion(
        self,
        run: WorkflowRun,
        ref: WorkflowRef,
        payload: dict[str, Any],
    ) -> WorkflowRun:
        work_id = payload.get("work_id")
        if not isinstance(work_id, str) or not work_id:
            _logger.warning("workflow completion missing work_id: workflow=%s", run.workflow_id)
            return run
        if work_id in run.seen_work_ids:
            return run
        if run.status in (
            WorkflowStatus.CANCELLED,
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.FAILED,
        ):
            await self._mutate(run.workflow_id, lambda value: value.seen_work_ids.add(work_id))
            return await self.require(run.workflow_id)
        node = next((item for item in run.definition.nodes if item.id == ref.node_id), None)
        node_run = run.nodes.get(ref.node_id)
        if node is None or node_run is None or ref.attempt != node_run.attempt_count:
            await self._mutate(run.workflow_id, lambda value: value.seen_work_ids.add(work_id))
            return await self.require(run.workflow_id)
        child_id = payload.get("conversation_id")
        cost = 0.0
        if isinstance(child_id, str):
            try:
                cumulative = max(0.0, float(await self._get_child_cost(child_id)))
                cost = max(0.0, cumulative - run.child_cost_usd.get(child_id, 0.0))
            except Exception:  # noqa: BLE001 - usage is observability, not correctness
                _logger.warning(
                    "failed to read workflow child cost: child=%s", child_id, exc_info=True
                )
        status = str(payload.get("status", "failed"))
        payload = await self._evaluate_result(payload)
        output = str(payload.get("output", ""))
        result: Any = None
        error: str | None = None
        if status == "completed":
            try:
                result = self._parse_result(output, node.output_schema)
            except ValueError as exc:
                error = str(exc)
        else:
            error = output or f"sub-agent finished with status {status}"
        retryable = error is not None and not self._non_retryable(error)

        def update(value: WorkflowRun) -> None:
            current = value.nodes[ref.node_id]
            value.seen_work_ids.add(work_id)
            value.spent_cost_usd += cost
            if isinstance(child_id, str):
                value.child_cost_usd[child_id] = value.child_cost_usd.get(child_id, 0.0) + cost
            self._release_cost(value, current)
            if current.attempts:
                attempt = current.attempts[-1]
                attempt.work_id = work_id
                attempt.status = status if error is None else "failed"
                attempt.error = error
                attempt.completed_at = time.time()
            if error is None:
                current.state = NodeState.SUCCEEDED
                current.result = result
                current.error = None
            elif retryable and current.attempt_count < node.max_attempts:
                current.state = NodeState.PENDING
                current.error = error
            else:
                current.state = NodeState.BLOCKED
                current.error = error
                value.status = WorkflowStatus.BLOCKED
                value.blocked_reason = f"node {node.id} failed: {error}"
            value.updated_at = time.time()

        updated = await self._mutate(run.workflow_id, update)
        self._emit(updated)
        if updated.status == WorkflowStatus.BLOCKED:
            await self._wake_terminal(updated)
        return updated

    async def _settle_workflow(self, run: WorkflowRun) -> None:
        if run.status != WorkflowStatus.RUNNING:
            return
        states = {node.state for node in run.nodes.values()}
        if states and states == {NodeState.SUCCEEDED}:

            def update(value: WorkflowRun) -> None:
                value.status = WorkflowStatus.SUCCEEDED
                value.updated_at = time.time()

            run = await self._mutate(run.workflow_id, update)
            self._emit(run)
            await self._wake_terminal(run)
        elif NodeState.BLOCKED in states:
            await self._block(run, run.blocked_reason or "workflow contains a blocked node")

    async def _block(self, run: WorkflowRun, reason: str) -> WorkflowRun:
        def update(value: WorkflowRun) -> None:
            value.status = WorkflowStatus.BLOCKED
            value.blocked_reason = reason
            value.updated_at = time.time()

        updated = await self._mutate(run.workflow_id, update)
        self._emit(updated)
        await self._wake_terminal(updated)
        return updated

    async def _wake_terminal(self, run: WorkflowRun) -> None:
        if run.terminal_wake_sent:
            return
        notice = (
            f"[System: workflow {run.workflow_id!r} is {run.status.value}. "
            "Call sys_workflow_get for the decision summary.]"
        )
        await self._wake_parent(notice)

        def update(value: WorkflowRun) -> None:
            value.terminal_wake_sent = True
            value.updated_at = time.time()

        updated = await self._mutate(run.workflow_id, update)
        self._emit(updated)

    async def _mutate(
        self, workflow_id: str, update: Callable[[WorkflowRun], None]
    ) -> WorkflowRun:
        try:
            return await self._store.mutate(self.parent_session_id, workflow_id, update)
        except KeyError as exc:
            raise KeyError(f"workflow {workflow_id!r} not found") from exc

    def _emit(self, run: WorkflowRun) -> None:
        self._publish(
            {
                "type": "session.workflow.updated",
                "conversation_id": self.parent_session_id,
                "workflow": run.summary(),
            }
        )

    def _reserve_cost(self, run: WorkflowRun, node: WorkflowNode) -> bool:
        workflow_limit = run.definition.budget.max_cost_usd
        reservation = node.cost_budget.max_cost_usd if node.cost_budget is not None else 0.0
        if workflow_limit is not None:
            if run.spent_cost_usd + run.reserved_cost_usd + reservation > workflow_limit:
                return False
        node_run = run.nodes[node.id]
        node_run.reserved_cost_usd = reservation
        run.reserved_cost_usd += reservation
        return True

    @staticmethod
    def _release_cost(run: WorkflowRun, node_run: WorkflowNodeRun) -> None:
        run.reserved_cost_usd = max(0.0, run.reserved_cost_usd - node_run.reserved_cost_usd)
        node_run.reserved_cost_usd = 0.0

    @staticmethod
    def _critical_path(definition: WorkflowDefinition, node_id: str) -> int:
        children: dict[str, list[str]] = {node.id: [] for node in definition.nodes}
        for node in definition.nodes:
            for dep in node.deps:
                children[dep].append(node.id)
        memo: dict[str, int] = {}

        def length(current: str) -> int:
            if current not in memo:
                memo[current] = 1 + max((length(child) for child in children[current]), default=0)
            return memo[current]

        return length(node_id)

    @staticmethod
    def _parse_result(output: str, schema: dict[str, Any]) -> Any:
        matches = _RESULT_RE.findall(output)
        if not matches:
            raise ValueError("child result did not include a <workflow_result> JSON block")
        try:
            result = json.loads(matches[-1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"child workflow_result was not valid JSON: {exc.msg}") from exc
        try:
            jsonschema.validate(result, schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(
                f"child workflow_result failed schema validation: {exc.message}"
            ) from exc
        return result

    @staticmethod
    def _non_retryable(error: str) -> bool:
        lowered = error.lower()
        return any(marker in lowered for marker in _NON_RETRYABLE_MARKERS)

    @staticmethod
    def _node_prompt(run: WorkflowRun, node: WorkflowNode, attempt: int) -> str:
        context = ""
        if node.deps:
            dep_outputs = {dep: run.nodes[dep].result for dep in node.deps}
            context = f"\nDependency outputs:\n{json.dumps(dep_outputs, sort_keys=True)}\n"
        workspace = (
            f"\nWork only in this existing worktree: {node.worktree_path}\n"
            if node.worktree_path
            else ""
        )
        retry = (
            "\nThis is a retry. Correct the previous failure and return a valid "
            "structured result.\n"
            if attempt > 1
            else ""
        )
        return (
            f"Workflow {run.workflow_id}, node {node.id}, role {node.role}.\n"
            f"Acceptance contract:\n{node.contract}\n"
            f"{context}{workspace}{retry}"
            "Finish with exactly one JSON object wrapped as:\n"
            "<workflow_result>\n{...}\n</workflow_result>\n"
            f"The JSON must satisfy this schema:\n{json.dumps(node.output_schema, sort_keys=True)}"
        )


_MANAGERS: dict[str, WorkflowManager] = {}


def get_workflow_manager(parent_session_id: str) -> WorkflowManager | None:
    return _MANAGERS.get(parent_session_id)


def register_workflow_manager(manager: WorkflowManager) -> WorkflowManager:
    existing = _MANAGERS.get(manager.parent_session_id)
    if existing is not None:
        return existing
    _MANAGERS[manager.parent_session_id] = manager
    return manager


async def remove_workflow_manager(parent_session_id: str) -> None:
    manager = _MANAGERS.pop(parent_session_id, None)
    if manager is not None:
        await manager.close()


def deliver_workflow_completion(
    parent_session_id: str,
    workflow_ref: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    manager = _MANAGERS.get(parent_session_id)
    if manager is None:
        return False
    try:
        ref = WorkflowRef(
            workflow_id=str(workflow_ref["workflow_id"]),
            node_id=str(workflow_ref["node_id"]),
            attempt=int(workflow_ref["attempt"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return manager.enqueue_completion(ref, payload)
