from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from omnigent.dag_workflows.models import WorkflowDefinition, WorkflowRuntimeConfig
from omnigent.dag_workflows.runtime import WorkflowManager, WorkflowRef


def _definition() -> dict[str, Any]:
    node = {
        "role": "generic",
        "contract": "Return the requested value.",
        "agent": "codex",
        "output_schema": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "integer"}},
            "additionalProperties": False,
        },
        "max_attempts": 2,
    }
    return {
        "id": "build",
        "name": "Build in dependency order",
        "budget": {"max_concurrency": 2, "max_dispatches": 8},
        "nodes": [
            {**node, "id": "a", "title": "A", "deps": []},
            {**node, "id": "b", "title": "B", "deps": []},
            {**node, "id": "c", "title": "C", "deps": ["a", "b"]},
        ],
    }


async def _wait_until(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_definition_rejects_cycles() -> None:
    raw = _definition()
    raw["nodes"][0]["deps"] = ["c"]
    with pytest.raises(ValidationError, match="cycle"):
        WorkflowDefinition.model_validate(raw)


@pytest.mark.asyncio
async def test_controller_schedules_dependencies_retries_and_wakes_once() -> None:
    dispatches: list[tuple[str, dict[str, Any], str | None]] = []
    wakes: list[str] = []

    async def dispatch(node: Any, _prompt: str, ref: dict[str, Any], existing: str | None):
        dispatches.append((node.id, ref, existing))
        return {"conversation_id": existing or f"conv_{node.id}"}

    async def noop(_value: str) -> None:
        return None

    async def wake(message: str) -> None:
        wakes.append(message)

    async def cost(_child: str) -> float:
        return 0.0

    async def result(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    manager = WorkflowManager(
        parent_session_id="conv_parent",
        limits=WorkflowRuntimeConfig(enabled=True),
        dispatch=dispatch,
        cancel_child=noop,
        wake_parent=wake,
        publish=lambda _event: None,
        get_child_cost=cost,
        evaluate_result=result,
    )
    try:
        draft = await manager.submit(_definition())
        await manager.start(draft.workflow_id, draft.definition.version, draft.definition_hash)
        await _wait_until(lambda: len(dispatches) == 2)
        assert {item[0] for item in dispatches} == {"a", "b"}

        manager.enqueue_completion(
            WorkflowRef("build", "a", 1),
            {
                "work_id": "work-a",
                "conversation_id": "conv_a",
                "status": "completed",
                "output": '<workflow_result>{"value": 1}</workflow_result>',
            },
        )
        manager.enqueue_completion(
            WorkflowRef("build", "b", 1),
            {
                "work_id": "work-b-invalid",
                "conversation_id": "conv_b",
                "status": "completed",
                "output": "missing structured result",
            },
        )
        await _wait_until(lambda: len(dispatches) == 3)
        assert dispatches[2][0] == "b"
        assert dispatches[2][2] == "conv_b"

        # A duplicate/stale completion cannot consume the retry or wake Polly.
        manager.enqueue_completion(
            WorkflowRef("build", "b", 1),
            {
                "work_id": "work-b-invalid",
                "conversation_id": "conv_b",
                "status": "completed",
                "output": '<workflow_result>{"value": 99}</workflow_result>',
            },
        )
        manager.enqueue_completion(
            WorkflowRef("build", "b", 2),
            {
                "work_id": "work-b-retry",
                "conversation_id": "conv_b",
                "status": "completed",
                "output": '<workflow_result>{"value": 2}</workflow_result>',
            },
        )
        await _wait_until(lambda: len(dispatches) == 4)
        assert dispatches[3][0] == "c"

        manager.enqueue_completion(
            WorkflowRef("build", "c", 1),
            {
                "work_id": "work-c",
                "conversation_id": "conv_c",
                "status": "completed",
                "output": '<workflow_result>{"value": 3}</workflow_result>',
            },
        )
        await _wait_until(lambda: len(wakes) == 1)
        run = await manager.require("build")
        assert run.status.value == "succeeded"
        assert run.dispatch_count == 4
        assert run.nodes["b"].attempt_count == 2
        assert run.nodes["c"].result == {"value": 3}
        assert len(wakes) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_blocked_node_requires_amendment_before_restart() -> None:
    dispatches: list[dict[str, Any]] = []

    async def dispatch(node: Any, _prompt: str, ref: dict[str, Any], existing: str | None):
        dispatches.append(ref)
        return {"conversation_id": existing or f"conv_{node.id}"}

    async def noop(_value: str) -> None:
        return None

    async def cost(_child: str) -> float:
        return 0.0

    async def result(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    definition = _definition()
    definition["nodes"] = [definition["nodes"][0]]
    definition["nodes"][0]["max_attempts"] = 1
    manager = WorkflowManager(
        parent_session_id="conv_parent",
        limits=WorkflowRuntimeConfig(enabled=True),
        dispatch=dispatch,
        cancel_child=noop,
        wake_parent=noop,
        publish=lambda _event: None,
        get_child_cost=cost,
        evaluate_result=result,
    )
    try:
        draft = await manager.submit(definition)
        await manager.start("build", 1, draft.definition_hash)
        await _wait_until(lambda: len(dispatches) == 1)
        manager.enqueue_completion(
            WorkflowRef("build", "a", 1),
            {
                "work_id": "failed-a",
                "conversation_id": "conv_a",
                "status": "completed",
                "output": "invalid",
            },
        )
        await _wait_until(
            lambda: len(dispatches) == 1 and manager._reconcile_tasks.get("build") is None
        )
        blocked = await manager.require("build")
        assert blocked.status.value == "blocked"
        with pytest.raises(ValueError, match="amend or remove"):
            await manager.start("build", 1, blocked.definition_hash)

        replacement = {**definition["nodes"][0], "contract": "Return a corrected value"}
        amended = await manager.amend("build", 1, {"replace_nodes": [replacement]})
        assert amended.nodes["a"].attempt_count == 0
        await manager.start("build", 2, amended.definition_hash)
        await _wait_until(lambda: len(dispatches) == 2)
        assert dispatches[-1]["attempt"] == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_late_completion_cannot_overwrite_cancelled_status() -> None:
    dispatches: list[dict[str, Any]] = []

    async def dispatch(node: Any, _prompt: str, ref: dict[str, Any], existing: str | None):
        dispatches.append(ref)
        return {"conversation_id": existing or f"conv_{node.id}"}

    async def noop(_value: str) -> None:
        return None

    async def cost(_child: str) -> float:
        return 0.0

    async def result(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    definition = _definition()
    definition["nodes"] = [definition["nodes"][0]]
    manager = WorkflowManager(
        parent_session_id="conv_parent",
        limits=WorkflowRuntimeConfig(enabled=True),
        dispatch=dispatch,
        cancel_child=noop,
        wake_parent=noop,
        publish=lambda _event: None,
        get_child_cost=cost,
        evaluate_result=result,
    )
    try:
        draft = await manager.submit(definition)
        await manager.start("build", 1, draft.definition_hash)
        await _wait_until(lambda: len(dispatches) == 1)
        await manager.cancel("build")
        manager.enqueue_completion(
            WorkflowRef("build", "a", 1),
            {
                "work_id": "late-a",
                "conversation_id": "conv_a",
                "status": "failed",
                "output": "late failure",
            },
        )
        await _wait_until(lambda: manager._reconcile_tasks.get("build") is None)
        run = await manager.require("build")
        assert run.status.value == "cancelled"
        assert "late-a" in run.seen_work_ids
    finally:
        await manager.close()
