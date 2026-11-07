"""Explicit, in-memory definitions shared by the workflow component tests."""

import pytest

from omnigent.workflows.models import AgentTeam, ManagedSandbox, TeamMember, Workflow, WorkflowNode


@pytest.fixture(autouse=True)
def enable_dynamic_workflows(monkeypatch):
    monkeypatch.setenv("OMNIGENT_FEATURES", "dynamic_workflows")


@pytest.fixture
def workflow() -> Workflow:
    return Workflow(
        name="test workflow",
        goal="Solve the task",
        teams=(
            AgentTeam(
                id="team",
                lead="lead",
                members=tuple(
                    TeamMember(
                        id=member_id,
                        role=member_id,
                        agent_id="ag_test",
                        placement=ManagedSandbox(provider="test-provider"),
                    )
                    for member_id in ("lead", "reviewer")
                ),
            ),
        ),
        nodes=(WorkflowNode(id="root", goal="Investigate", team="team", allow_expansion=True),),
        max_parallel_members=2,
    )


@pytest.fixture
def owner_workflow(workflow: Workflow) -> Workflow:
    return Workflow.model_validate(
        {
            **workflow.model_dump(),
            "teams": [{**workflow.teams[0].model_dump(), "strategy": "owner"}],
        }
    )
