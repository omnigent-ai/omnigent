"""Owner rounds, independent evidence, and event ordering at scheduler boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnigent.workflows import (
    Artifact,
    CommandGate,
    ExternalGate,
    FeedbackEvent,
    GateEvent,
    GateOutcome,
    MemberRequest,
    NodeResult,
    RunStore,
    Workflow,
    WorkflowNode,
    WorkflowRunner,
)
from omnigent.workflows.gates import GateRequest


def _candidate(revision: str) -> str:
    return NodeResult(
        summary=f"Candidate {revision}",
        outputs={"report": {"revision": revision, "cases": ["normal", "edge"], "passed": True}},
        artifacts=(Artifact(id="result", kind="report", uri="test://report", revision=revision),),
    ).model_dump_json()


async def test_owner_consults_then_repairs_before_expansion_and_join(
    owner_workflow: Workflow, tmp_path: Path
) -> None:
    gate = CommandGate(
        id="tests",
        artifacts=("result",),
        command=(
            sys.executable,
            "-c",
            "import json,sys; candidate=json.load(sys.stdin); "
            "print(candidate['candidate_id']); "
            "sys.exit(candidate['artifacts'][0]['revision'] != 'revision-3')",
        ),
        cwd=str(tmp_path),
    )
    workflow = owner_workflow.model_copy(
        update={
            "nodes": (
                owner_workflow.nodes[0].model_copy(update={"gates": (gate,)}),
                WorkflowNode(id="join", goal="Join", team="team", depends_on=("root",)),
            )
        }
    )
    calls: list[tuple[str, str, int]] = []
    sessions: list[str] = []

    async def execute(request: MemberRequest) -> str:
        calls.append((request.node.id, request.member.id, request.round))
        if request.record.session_id is None:
            request.record.session_id = f"session-{request.node.id}-{request.member.id}"
            sessions.append(request.record.session_id)
            request.checkpoint()
        if request.node.id != "root":
            assert '"outputs": {"report":' in request.prompt
            assert runner.nodes["root"].outputs["report"]["revision"] == "revision-3"
            assert runner.nodes["root"].artifacts[0].revision == "revision-3"
            assert '"revision": "revision-3"' in request.prompt
            return '{"summary":"done"}'
        assert "extra" not in runner.nodes
        assert runner.nodes["join"].status == "pending"
        if request.member.id == "reviewer":
            assert "Review edge cases" in request.prompt
            return "specialist evidence"
        if request.round == 1:
            return NodeResult(
                summary="Review edge cases", status="consult", consult_members=("reviewer",)
            ).model_dump_json()
        assert "specialist evidence" in request.prompt
        if request.round == 3:
            assert "Validation failed" in request.prompt and "root-2" in request.prompt
        return (
            NodeResult.model_validate_json(_candidate(f"revision-{request.round}"))
            .model_copy(
                update={"add_nodes": (WorkflowNode(id="extra", goal="Extra", team="team"),)}
            )
            .model_dump_json()
        )

    runner = WorkflowRunner(workflow, execute)
    await runner.run()
    assert runner.status == "succeeded", runner.snapshot()
    assert calls[:4] == [
        ("root", "lead", 1),
        ("root", "reviewer", 2),
        ("root", "lead", 2),
        ("root", "lead", 3),
    ]
    assert sessions.count("session-root-lead") == 1
    assert all(member == "lead" for node, member, _ in calls if node != "root")
    assert [
        (result.candidate_id, result.outcome.passed)
        for result in runner.nodes["root"].gate_results
    ] == [("root-2", False), ("root-3", True)]
    assert runner.workflow.nodes[-1].parent == "root"
    assert runner.nodes["root"].artifacts[0].revision == "revision-3"


async def test_external_evidence_is_revision_specific_and_deduplicated(
    owner_workflow: Workflow, tmp_path: Path
) -> None:
    workflow = owner_workflow.model_copy(
        update={
            "nodes": (
                owner_workflow.nodes[0].model_copy(
                    update={"gates": (ExternalGate(id="tests", artifacts=("result",)),)}
                ),
                WorkflowNode(id="join", goal="Join", team="team", depends_on=("root",)),
                WorkflowNode(id="other", goal="Independent", team="team"),
            )
        }
    )
    calls: list[str] = []

    async def execute(request: MemberRequest) -> str:
        calls.append(request.turn_id)
        if request.node.id == "join":
            assert '"outputs": {"report":' in request.prompt
            assert runner.nodes["root"].outputs == {
                "report": {"revision": "revision-2", "cases": ["normal", "edge"], "passed": True}
            }
            assert '"revision": "revision-2"' in request.prompt
        return _candidate(f"revision-{request.round}")

    store = RunStore(tmp_path)
    runner = WorkflowRunner(workflow, execute, store=store)
    await runner.run()
    assert runner.status == "waiting"
    assert runner.nodes["other"].status == "succeeded"
    assert runner.nodes["root"].outputs == {}
    assert runner.nodes["root"].candidate.outputs["report"]["revision"] == "revision-1"
    assert runner.nodes["join"].status == "pending"
    failed = GateEvent(
        id="failed",
        node_id="root",
        candidate_id="root-1",
        gate_id="tests",
        outcome=GateOutcome(passed=False, detail="Fix the edge case"),
    )
    store.enqueue(failed)
    store.enqueue(failed)
    with pytest.raises(ValueError, match="Conflicting"):
        store.enqueue(
            failed.model_copy(update={"outcome": GateOutcome(passed=True, detail="changed")})
        )
    runner = WorkflowRunner.restore(store.load(), execute, store=store)
    await runner.run()
    assert runner.nodes["root"].candidate_id == "root-2"
    assert runner.nodes["root"].outputs == {}
    assert runner.nodes["join"].status == "pending"
    store.enqueue(
        failed.model_copy(
            update={"id": "stale", "outcome": GateOutcome(passed=True, detail="old pass")}
        )
    )
    await runner.run()
    assert runner.events["stale"].status == "rejected"
    assert runner.status == "waiting"
    passed = GateEvent(
        id="passed",
        node_id="root",
        candidate_id="root-2",
        gate_id="tests",
        outcome=GateOutcome(passed=True, detail="Checks passed", evidence="test://checks/2"),
    )
    store.enqueue(passed)
    store.enqueue(passed.model_copy(update={"id": "already-settled"}))
    runner = WorkflowRunner.restore(store.load(), execute, store=store)
    await runner.run()
    assert runner.status == "succeeded"
    assert runner.events["already-settled"].status == "rejected"
    assert runner.events["failed"].status == runner.events["passed"].status == "applied"
    restored = WorkflowRunner.restore(store.load(), execute)
    assert restored.nodes["root"].outputs == runner.nodes["root"].outputs
    assert calls == [
        "root.1.work.lead",
        "other.1.work.lead",
        "root.2.work.lead",
        "join.1.work.lead",
    ]


@pytest.mark.parametrize("during", ["owner", "gate", "waiting"])
async def test_feedback_invalidates_candidate_before_releasing_dependants(
    owner_workflow: Workflow, tmp_path: Path, during: str
) -> None:
    gate = CommandGate(id="tests", command=("unused",), cwd=str(tmp_path))
    workflow = owner_workflow.model_copy(
        update={
            "nodes": (
                owner_workflow.nodes[0].model_copy(update={"gates": (gate,)}),
                WorkflowNode(id="join", goal="Join", team="team", depends_on=("root",)),
            )
        }
    )
    store = RunStore(tmp_path)

    def feedback() -> None:
        for number in (1, 2):
            store.enqueue(
                FeedbackEvent(
                    id=f"feedback-{number}", node_id="root", message=f"adjustment-{number}"
                )
            )

    async def execute(request: MemberRequest) -> str:
        if request.node.id == "root" and request.round == 1:
            if during == "owner":
                feedback()
                await asyncio.sleep(0)
            elif during == "waiting":
                return '{"summary":"Need a decision", "status":"waiting"}'
        if request.node.id == "root" and request.round == 2:
            assert request.prompt.index("adjustment-1") < request.prompt.index("adjustment-2")
        if request.node.id == "join":
            assert runner.nodes["root"].artifacts[0].revision == "revision-2"
        return _candidate(f"revision-{request.round}")

    async def validate(request: GateRequest) -> GateOutcome:
        if during == "gate" and request.candidate_id == "root-1":
            feedback()
            await asyncio.sleep(0)
        return GateOutcome(passed=True, detail="pass")

    runner = WorkflowRunner(workflow, execute, store=store, validate=validate)
    await runner.run()
    if during == "waiting":
        assert runner.nodes["root"].status == "waiting"
        feedback()
        await runner.run()
    assert runner.status == "succeeded", runner.snapshot()
    assert runner.nodes["root"].round == 2
    assert len(runner.nodes["root"].members["lead"].turns) == 2
    assert all(event.status == "applied" for event in runner.events.values())


@pytest.mark.parametrize("max_rounds", [1, 2, 3])
async def test_missing_artifacts_exhaust_rounds_without_running_check_or_expanding(
    owner_workflow: Workflow, tmp_path: Path, max_rounds: int
) -> None:
    workflow = owner_workflow.model_copy(
        update={
            "nodes": (
                owner_workflow.nodes[0].model_copy(
                    update={
                        "max_rounds": max_rounds,
                        "gates": (
                            CommandGate(
                                id="tests",
                                artifacts=("required",),
                                command=("unused",),
                                cwd=str(tmp_path),
                            ),
                        ),
                    }
                ),
                WorkflowNode(id="join", goal="Join", team="team", depends_on=("root",)),
            )
        }
    )

    async def execute(request: MemberRequest) -> str:
        return '{"summary":"done", "add_nodes":[{"id":"extra","team":"team","goal":"Extra"}]}'

    async def validate(request: GateRequest) -> GateOutcome:
        pytest.fail("A check must not run against missing artifacts")

    runner = WorkflowRunner(workflow, execute, validate=validate)
    await runner.run()
    assert runner.status == "failed"
    assert f"max_rounds={max_rounds}" in runner.nodes["root"].error
    assert runner.nodes["root"].round == max_rounds
    assert runner.nodes["join"].status == "blocked"
    assert "extra" not in runner.nodes
    assert len(runner.nodes["root"].gate_results) == max_rounds
    assert all(
        "Missing required artifacts" in gate.outcome.detail
        for gate in runner.nodes["root"].gate_results
    )


@pytest.mark.parametrize("member", ["lead", "unknown"])
async def test_owner_cannot_consult_itself_or_unknown_specialist(
    owner_workflow: Workflow, member: str
) -> None:
    async def execute(request: MemberRequest) -> str:
        return json.dumps({"summary": "consult", "status": "consult", "consult_members": [member]})

    runner = WorkflowRunner(owner_workflow, execute)
    await runner.run()
    assert runner.status == "failed"
    assert "unknown specialist or itself" in runner.nodes["root"].error


async def test_agent_expansion_cannot_introduce_controller_commands(
    owner_workflow: Workflow, tmp_path: Path
) -> None:
    async def execute(request: MemberRequest) -> str:
        return NodeResult(
            summary="done",
            add_nodes=(
                WorkflowNode(
                    id="extra",
                    goal="Extra",
                    team="team",
                    gates=(CommandGate(id="run", command=("unused",), cwd=str(tmp_path)),),
                ),
            ),
        ).model_dump_json()

    runner = WorkflowRunner(owner_workflow, execute)
    await runner.run()
    assert runner.status == "failed"
    assert "cannot introduce controller command gates" in runner.nodes["root"].error
    assert "extra" not in runner.nodes


@pytest.mark.parametrize("parent", ["unknown", "child"])
def test_containment_must_reference_an_acyclic_parent(
    owner_workflow: Workflow, parent: str
) -> None:
    with pytest.raises(ValidationError, match=r"unknown parent|cycle"):
        Workflow.model_validate(
            {
                **owner_workflow.model_dump(),
                "nodes": (
                    *owner_workflow.nodes,
                    WorkflowNode(id="child", goal="Child", team="team", parent=parent),
                ),
            }
        )


async def test_containment_does_not_imply_execution_order(owner_workflow: Workflow) -> None:
    workflow = owner_workflow.model_copy(
        update={
            "nodes": (
                owner_workflow.nodes[0].model_copy(update={"depends_on": ("child",)}),
                WorkflowNode(id="child", goal="Child", team="team", parent="root"),
            )
        }
    )
    calls = []

    async def execute(request: MemberRequest) -> str:
        calls.append(request.node.id)
        return '{"summary":"done"}'

    runner = WorkflowRunner(workflow, execute)
    await runner.run()
    assert runner.status == "succeeded"
    assert calls == ["child", "root"]


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": "bad", "status": "consult"},
        {"summary": "bad", "outputs": {"large": "a" * 65536}},
        {"summary": "bad", "outputs": {"not an identifier": 1}},
        {"summary": "bad", "outputs": {"number": float("nan")}},
        {"summary": "bad", "outputs": {"data": object()}},
        {"summary": "bad", "consult_members": ["reviewer"]},
        {"summary": "bad", "status": "consult", "consult_members": ["reviewer", "reviewer"]},
        {
            "summary": "bad",
            "status": "waiting",
            "add_nodes": [{"id": "extra", "goal": "Extra", "team": "team"}],
        },
        {
            "summary": "bad",
            "artifacts": [{"id": "a", "kind": "report", "uri": "test://a", "revision": "1"}] * 2,
        },
    ],
)
def test_invalid_owner_result_contracts(payload: dict) -> None:
    with pytest.raises(ValidationError):
        NodeResult.model_validate(payload)
