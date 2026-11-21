from __future__ import annotations

import httpx
import pytest

from omnigent.runner.native.orchestration import _native_startup_instructions
from omnigent.runner.project_orchestrator import (
    PROJECT_ORCHESTRATOR_INSTRUCTION,
    append_project_orchestrator_instructions,
)
from omnigent.runtime.project_ticket_instructions import PROJECT_TICKET_WORK_INSTRUCTION
from omnigent.spec.types import AgentSpec, ToolGroupsConfig


@pytest.mark.parametrize("role", [True, False, None, "true"])
@pytest.mark.parametrize("project_id", [None, "project"])
async def test_project_guidance_and_designated_role_in_native_and_sdk_prompts(role, project_id):
    spec = AgentSpec(
        spec_version=1,
        instructions="Shared agent instructions",
        tool_groups=ToolGroupsConfig(memory=False),
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/sessions/session"
        assert dict(request.url.params) == {
            "include_items": "false",
            "include_liveness": "false",
            "include_live_status": "false",
            "include_usage": "false",
        }
        return httpx.Response(200, json={"project_orchestrator": role, "project_id": project_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server.test"
    ) as client:
        native = await _native_startup_instructions(spec, client, "session")
        sdk = await append_project_orchestrator_instructions(
            "Shared agent instructions\n\nRequest instructions", client, "session", spec
        )
    assert len(requests) == 2
    assert native.startswith(spec.instructions)
    assert sdk.startswith("Shared agent instructions\n\nRequest instructions")
    for prompt in (native, sdk):
        assert (PROJECT_TICKET_WORK_INSTRUCTION in prompt) is (project_id is not None)
        assert (PROJECT_ORCHESTRATOR_INSTRUCTION in prompt) is (role is True)
    assert spec.instructions == "Shared agent instructions"


def test_stage_fit_contract_requires_a_decision_without_phase_or_approval_shortcuts():
    assert "stage fit" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "actual work and next step" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "ticket owner" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "never grant approval" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "substantive progress" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "stage_id and a concise evidence-based comment in the same call" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "helpers report mismatches" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "implementation even when the code is uncommitted" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "Keep needs-human until its request is resolved" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "existing monitoring checks" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "resolve an ambiguous boundary" in PROJECT_TICKET_WORK_INSTRUCTION


async def test_agent_without_project_tools_has_no_role_or_server_lookup():
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=False))

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), base_url="http://server.test"
    ) as client:
        assert (
            await append_project_orchestrator_instructions("Author", client, "session", spec)
            == "Author"
        )
    assert (
        await append_project_orchestrator_instructions("Author", None, "session", spec) == "Author"
    )


async def test_unavailable_role_lookup_does_not_silently_start_an_ordinary_worker():
    spec = AgentSpec(spec_version=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        base_url="http://server.test",
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await append_project_orchestrator_instructions("Author", client, "session", spec)
