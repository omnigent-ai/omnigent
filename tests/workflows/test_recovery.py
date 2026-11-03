"""Checkpoint boundaries must not silently replay remote actions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from omnigent.workflows import (
    FeedbackEvent,
    MemberRequest,
    ResolveTurnEvent,
    RunStore,
    Workflow,
    WorkflowRunner,
)
from omnigent.workflows.state import ExecutionBinding


@pytest.mark.parametrize("known_session", [True, False])
@pytest.mark.parametrize("mode", ["server", "arclet"])
async def test_uncertain_turn_waits_for_reconciliation_without_resubmission(
    owner_workflow: Workflow, tmp_path: Path, known_session: bool, mode: str
) -> None:
    store = RunStore(tmp_path)
    started = asyncio.Event()
    calls = []

    async def execute(request: MemberRequest) -> str:
        calls.append(request.turn_id)
        if known_session:
            request.record.session_id = "session-known"
        request.checkpoint()
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    runner = WorkflowRunner(
        owner_workflow,
        execute,
        store=store,
        binding=ExecutionBinding.model_validate({"mode": mode, "server": "http://test"}),
    )
    task = asyncio.create_task(runner.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runner = WorkflowRunner.restore(store.load(), execute, store=store)
    await runner.run()
    assert runner.status == "waiting"
    assert runner.nodes["root"].status == "interrupted"
    assert calls == ["root.1.work.lead"]
    store.enqueue(
        ResolveTurnEvent(
            id="wrong-session",
            node_id="root",
            member_id="lead",
            turn_id=calls[0],
            session_id="wrong" if known_session else None,
            output='{"summary":"done"}',
        )
    )
    await runner.run()
    assert runner.events["wrong-session"].status == "rejected"
    store.enqueue(
        ResolveTurnEvent(
            id="resolved",
            node_id="root",
            member_id="lead",
            turn_id=calls[0],
            session_id="session-known",
            output='{"summary":"Recovered result"}',
        )
    )
    runner = WorkflowRunner.restore(store.load(), execute, store=store)
    await runner.run()
    assert runner.status == "succeeded", runner.snapshot()
    assert runner.nodes["root"].summary == "Recovered result"
    assert runner.nodes["root"].members["lead"].session_id == "session-known"
    assert runner.events["resolved"].status == "applied"
    assert calls == ["root.1.work.lead"]


async def test_saved_completed_turn_is_reused_before_candidate_commit(
    owner_workflow: Workflow,
    tmp_path: Path,
) -> None:
    snapshots = []

    async def execute(request: MemberRequest) -> str:
        request.record.session_id = "saved-session"
        return '{"summary":"done"}'

    class CaptureStore(RunStore):
        def save(self, snapshot: dict) -> None:
            snapshots.append(snapshot)
            super().save(snapshot)

    runner = WorkflowRunner(owner_workflow, execute, store=CaptureStore(tmp_path))
    await runner.run()
    saved = next(
        snapshot
        for snapshot in snapshots
        if (
            snapshot["nodes"]["root"]["candidate"] is None
            and snapshot["nodes"]["root"]["members"].get("lead", {}).get("output")
        )
    )

    async def must_not_execute(request: MemberRequest) -> str:
        pytest.fail("A completed turn was replayed")

    restored = WorkflowRunner.restore(saved, must_not_execute)
    await restored.run()
    assert restored.status == "succeeded"
    assert restored.nodes["root"].summary == "done"
    assert restored.nodes["root"].members["lead"].session_id == "saved-session"


async def test_controller_lock_and_stale_restore_prevent_competing_writes(
    owner_workflow: Workflow, tmp_path: Path
) -> None:
    async def execute(request: MemberRequest) -> str:
        return '{"summary":"waiting", "status":"waiting"}'

    store = RunStore(tmp_path)
    runner = WorkflowRunner(owner_workflow, execute, store=store)
    with FileLock(tmp_path / "controller.lock"):
        with pytest.raises(Timeout):
            await runner.run()
    assert not store.path.exists()
    await runner.run()
    original = store.load()
    stale = WorkflowRunner.restore(original, execute, store=store)
    store.enqueue(FeedbackEvent(id="feedback", node_id="root", message="Continue"))
    await runner.run()
    with pytest.raises(ValueError, match="changed"):
        await stale.run()
    with pytest.raises(ValueError, match="already exists"):
        await WorkflowRunner(owner_workflow, execute, store=store).run()
    assert store.load() == runner.snapshot()
    with pytest.raises(ValueError, match="nodes do not match"):
        WorkflowRunner.restore({**original, "nodes": {}}, execute)


async def test_failed_node_with_cancelled_teammate_stays_terminal(workflow: Workflow) -> None:
    started = asyncio.Event()

    async def execute(request: MemberRequest) -> str:
        if request.member.id == "lead":
            await started.wait()
            raise RuntimeError("failed")
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    runner = WorkflowRunner(workflow, execute)
    await runner.run()
    restored = WorkflowRunner.restore(runner.snapshot(), execute)
    assert restored.status == "failed"
    assert restored.nodes["root"].status == "failed"


async def test_checkpoint_failure_cannot_release_dependants(
    owner_workflow: Workflow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path)

    async def execute(request: MemberRequest) -> str:
        def fail_save(snapshot: dict) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "save", fail_save)
        return '{"summary":"done"}'

    runner = WorkflowRunner(owner_workflow, execute, store=store)
    with pytest.raises(OSError, match="disk full"):
        await runner.run()
    assert runner.status == "cancelled"
    saved = store.load()
    assert (
        saved["nodes"]["root"]["members"]["lead"]["turns"]["root.1.work.lead"]["status"]
        == "running"
    )
    restored = WorkflowRunner.restore(saved, execute)
    assert restored.nodes["root"].status == "interrupted"


async def test_pending_event_order_survives_checkpoint_serialization(
    owner_workflow: Workflow, tmp_path: Path
) -> None:
    async def execute(request: MemberRequest) -> str:
        return '{"summary":"done"}'

    runner = WorkflowRunner(owner_workflow, execute)
    for identifier in ("z-first", "a-second"):
        runner.enqueue(FeedbackEvent(id=identifier, node_id="root", message=identifier))
    store = RunStore(tmp_path)
    store.save(runner.snapshot())
    restored = WorkflowRunner.restore(store.load(), execute, store=store)
    await restored.run()
    assert restored.status == "succeeded"
    assert restored.nodes["root"].feedback == ["z-first\na-second"]
    assert list(restored.events) == ["z-first", "a-second"]
    saved = restored.snapshot()
    saved["events"].append(saved["events"][0])
    with pytest.raises(ValueError, match="duplicate event IDs"):
        WorkflowRunner.restore(saved, execute)


@pytest.mark.parametrize("contents", [[], {"unknown": []}, {"events": {}}])
def test_store_rejects_malformed_inbox(tmp_path: Path, contents: object) -> None:
    store = RunStore(tmp_path)
    (tmp_path / "inbox.json").write_text(json.dumps(contents))
    with pytest.raises(ValueError, match="Invalid workflow inbox"):
        store.events()
