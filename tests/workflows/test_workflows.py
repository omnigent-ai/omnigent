"""Graph invariants and scheduling belong below process and provider boundaries."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from omnigent.workflows.engine import MemberRequest, WorkflowRunner
from omnigent.workflows.models import Workflow, WorkflowNode


def test_expansion_requires_a_known_source(workflow: Workflow) -> None:
    with pytest.raises(ValueError, match="Unknown expansion source"):
        workflow.expand("missing", ())


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"nodes": []}, "at least 1"),
        ({"nodes": [{"id": "bad", "team": "missing", "goal": "task"}]}, "unknown team"),
        (
            {"nodes": [{"id": "bad", "team": "team", "goal": "task", "depends_on": ["missing"]}]},
            "unknown dependencies",
        ),
        (
            {"nodes": [{"id": "bad", "team": "team", "goal": "task", "depends_on": ["bad"]}]},
            "cycle",
        ),
        ({"max_parallel_members": 0}, "greater than or equal to 1"),
        ({"member_timeout_seconds": float("inf")}, "finite"),
        ({"unexpected": True}, "Extra inputs"),
    ],
)
def test_invalid_definitions_fail_before_execution(
    workflow: Workflow, updates: dict, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Workflow.model_validate({**workflow.model_dump(), **updates})


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("duplicate_nodes", "Duplicate node"),
        ("duplicate_teams", "Duplicate team"),
        ("duplicate_members", "duplicate member"),
        ("unknown_lead", "unknown lead"),
        ("cycle", "cycle"),
        ("size", "max_nodes"),
    ],
)
def test_graph_and_team_invariants(workflow: Workflow, mutation: str, message: str) -> None:
    data = workflow.model_dump()
    if mutation == "duplicate_nodes":
        data["nodes"] = (*data["nodes"], data["nodes"][0])
    elif mutation == "duplicate_teams":
        data["teams"] = (*data["teams"], data["teams"][0])
    elif mutation == "duplicate_members":
        data["teams"][0]["members"] = (data["teams"][0]["members"][0],) * 2
    elif mutation == "unknown_lead":
        data["teams"][0]["lead"] = "missing"
    else:
        data["nodes"] = (
            *data["nodes"],
            {"id": "next", "goal": "task", "team": "team", "depends_on": ["root"]},
        )
        if mutation == "cycle":
            data["nodes"][0]["depends_on"] = ["next"]
        else:
            data["max_nodes"] = 1
    with pytest.raises(ValidationError, match=message):
        Workflow.model_validate(data)


async def test_parallel_branches_join_after_expansion_with_all_results(workflow: Workflow) -> None:
    workflow = Workflow.model_validate(
        {
            **workflow.model_dump(),
            "nodes": (
                *workflow.nodes,
                WorkflowNode(id="other", team="team", goal="Independent task"),
                WorkflowNode(id="join", team="team", goal="Combine", depends_on=("root", "other")),
            ),
        }
    )
    active = maximum = 0
    calls: list[MemberRequest] = []
    two_active = asyncio.Event()
    root_waiting = asyncio.Event()
    other_started = asyncio.Event()

    async def execute(request: MemberRequest) -> str:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            two_active.set()
        try:
            if request.phase == "contribute" and request.node.id == "root":
                await two_active.wait()
                root_waiting.set()
            if request.node.id == "other":
                other_started.set()
            if request.phase == "synthesize" and request.node.id == "root":
                await other_started.wait()
            calls.append(request)
            if request.phase == "contribute":
                if request.node.id == "join":
                    assert all(
                        runner.nodes[parent].status == "succeeded"
                        for parent in ("root", "other", "check")
                    )
                    assert "summary-check" in request.prompt
                return f"evidence-{request.member.id}"
            assert "evidence-lead" in request.prompt and "evidence-reviewer" in request.prompt
            additions = (
                [{"id": "check", "goal": "Check", "team": "team"}]
                if request.node.id == "root"
                else []
            )
            return json.dumps({"summary": f"summary-{request.node.id}", "add_nodes": additions})
        finally:
            active -= 1

    runner = WorkflowRunner(workflow, execute)
    async with asyncio.timeout(3):
        report = await runner.run()
    assert report["status"] == "succeeded"
    assert root_waiting.is_set() and other_started.is_set()
    assert maximum == 2 and active == 0
    assert len(calls) == 12
    assert next(node for node in runner.workflow.nodes if node.id == "join").depends_on == (
        "root",
        "other",
        "check",
    )
    with pytest.raises(RuntimeError, match="once"):
        await runner.run()


@pytest.mark.parametrize(
    "addition, settings, message",
    [
        ({"id": "root", "team": "team", "goal": "replace"}, {}, "replace"),
        ({"id": "new", "team": "unknown", "goal": "bad"}, {}, "unknown team"),
        ({"id": "new", "team": "team", "goal": "cycle", "depends_on": ["join"]}, {}, "cycle"),
        ({"id": "new", "team": "team", "goal": "overflow"}, {"max_nodes": 3}, "max_nodes"),
    ],
)
async def test_invalid_expansion_is_atomic_and_blocks_only_descendants(
    workflow: Workflow,
    addition: dict,
    settings: dict,
    message: str,
) -> None:
    workflow = Workflow.model_validate(
        {
            **workflow.model_dump(),
            **settings,
            "nodes": (
                *workflow.nodes,
                WorkflowNode(id="join", goal="Join", team="team", depends_on=("root",)),
                WorkflowNode(id="other", goal="Independent", team="team"),
            ),
        }
    )

    async def execute(request: MemberRequest) -> str:
        return json.dumps(
            {"summary": "done", "add_nodes": [addition] if request.node.id == "root" else []}
        )

    runner = WorkflowRunner(workflow, execute)
    await runner.run()
    assert runner.status == "failed"
    assert runner.workflow == workflow
    assert runner.nodes["root"].status == "failed"
    assert message in runner.nodes["root"].error
    assert runner.nodes["join"].status == "blocked"
    assert runner.nodes["other"].status == "succeeded"


@pytest.mark.parametrize(
    "failure", ["exception", "invalid_json", "empty_summary", "expansion_disabled", "timeout"]
)
async def test_failures_are_recorded(workflow: Workflow, failure: str) -> None:
    workflow = Workflow.model_validate(
        {
            **workflow.model_dump(),
            "member_timeout_seconds": 0.02,
            "nodes": [{**workflow.nodes[0].model_dump(), "allow_expansion": False}],
        }
    )

    async def execute(request: MemberRequest) -> str:
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "exception":
            raise RuntimeError("member broke")
        if request.phase == "contribute":
            return "evidence"
        if failure == "invalid_json":
            return "not JSON"
        if failure == "empty_summary":
            return '{"summary": " "}'
        return '{"summary": "done", "add_nodes": [{"id": "new", "team": "team", "goal": "extra"}]}'

    runner = WorkflowRunner(workflow, execute)
    await runner.run()
    assert runner.nodes["root"].status == "failed"
    assert runner.nodes["root"].error
    assert set(runner.nodes) == {"root"}
    assert all(
        member.status not in {"running", "pending"}
        for member in runner.nodes["root"].members.values()
    )


async def test_failure_cancels_sibling_without_synthesis(workflow: Workflow) -> None:
    sibling_started = asyncio.Event()
    sibling_stopped = asyncio.Event()

    async def execute(request: MemberRequest) -> str:
        assert request.phase == "contribute"
        if request.member.id == "lead":
            await sibling_started.wait()
            raise RuntimeError("lead failed")
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_stopped.set()
        return "unreachable"

    runner = WorkflowRunner(workflow, execute)
    await runner.run()
    assert sibling_stopped.is_set()
    assert runner.nodes["root"].members["reviewer"].status == "cancelled"


async def test_cancellation_drains_active_and_queued_members(workflow: Workflow) -> None:
    workflow = Workflow.model_validate({**workflow.model_dump(), "max_parallel_members": 1})
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def execute(request: MemberRequest) -> str:
        request.record.session_id = "session-to-inspect"
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "unreachable"

    runner = WorkflowRunner(workflow, execute)
    task = asyncio.create_task(runner.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert runner.status == "cancelled"
    assert runner.nodes["root"].members["lead"].session_id == "session-to-inspect"
    assert {member.status for member in runner.nodes["root"].members.values()} == {"cancelled"}
