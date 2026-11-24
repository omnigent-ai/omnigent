from __future__ import annotations

import httpx
import pytest

from omnigent.runner.native.orchestration import _native_startup_instructions
from omnigent.runner.project_orchestrator import (
    PROJECT_ORCHESTRATOR_INSTRUCTION,
    append_project_instructions,
    project_lead_charter_instruction,
    project_mandate_instruction,
)
from omnigent.runtime.project_ticket_instructions import (
    PROJECT_CONVERSATION_INSTRUCTION,
    PROJECT_TICKET_WORK_INSTRUCTION,
)
from omnigent.spec.types import AgentSpec, ToolGroupsConfig
from omnigent.tools.builtins.project_tickets import SysTicketCreateTool


@pytest.mark.parametrize("role", [True, False, None, "true"])
@pytest.mark.parametrize("project_id", [None, "project"])
@pytest.mark.parametrize("role_field", ["project_main_session", "project_orchestrator"])
async def test_project_guidance_and_designated_role_in_native_and_sdk_prompts(
    role, project_id, role_field
):
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
            "include_project_mandate": "true",
        }
        return httpx.Response(200, json={role_field: role, "project_id": project_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server.test"
    ) as client:
        native = await _native_startup_instructions(spec, client, "session")
        sdk = await append_project_instructions(
            "Shared agent instructions\n\nRequest instructions", client, "session", spec
        )
    assert len(requests) == 2
    assert native.startswith(spec.instructions)
    assert sdk.startswith("Shared agent instructions\n\nRequest instructions")
    for prompt in (native, sdk):
        assert (PROJECT_CONVERSATION_INSTRUCTION in prompt) is (project_id is not None)
        assert (PROJECT_TICKET_WORK_INSTRUCTION in prompt) is (project_id is not None)
        assert (PROJECT_ORCHESTRATOR_INSTRUCTION in prompt) is (role is True)
    assert spec.instructions == "Shared agent instructions"


def test_comment_guidance_keeps_decisions_local_and_requires_real_message_targets():
    assert "lead with the outcome or current blocker" in PROJECT_TICKET_WORK_INSTRUCTION
    assert (
        "decisions, asks and next actions in the ticket itself" in PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "one short status sentence plus the important next action or ask" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "separate short paragraphs or bullets" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "**Status**, **Blocked** and **Next** when helpful, not as a required template" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "Do not pack setup, diagnostics and next steps into one dense paragraph" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "write a transcript of tool activity" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "A worker's comments default to agent notes for the coordinator" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "unless the reader needs them to act" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "State a blocker's impact and the exact action needed in the ticket" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "short responsibility phrase" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "stage owner" not in PROJECT_TICKET_WORK_INSTRUCTION.replace("-", " ")
    assert "put the full task checklist in the linked chat" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "durable message link is known" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "Never invent message IDs" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "session-only link as a specific-message link" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "keep the necessary detail in the comment" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "Preserve earlier comments and their provenance" in PROJECT_TICKET_WORK_INSTRUCTION


def test_stage_fit_contract_requires_a_decision_without_phase_or_approval_shortcuts():
    assert "ordinary comments, not dedicated" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "There is no built-in planning or review gate" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "responsible agent's chat" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "stage fit" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "actual work and next step" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "ticket owner" not in PROJECT_TICKET_WORK_INSTRUCTION
    assert "never grant approval" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "substantive progress" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "stage_id and a concise evidence-based comment in the same call" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "Coordinate stage changes with other agents working on the same ticket" in (
        PROJECT_TICKET_WORK_INSTRUCTION
    )
    assert "helpers report mismatches" not in PROJECT_TICKET_WORK_INSTRUCTION
    assert "implementation even when the code is uncommitted" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "Keep needs-human until its request is resolved" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "existing monitoring checks" in PROJECT_TICKET_WORK_INSTRUCTION
    assert "resolve an ambiguous boundary" in PROJECT_TICKET_WORK_INSTRUCTION


def test_conversation_in_any_project_session_creates_no_ticket_unless_work_persists():
    assert "project session is ordinary conversation" in PROJECT_CONVERSATION_INSTRUCTION
    assert "orchestrator" not in PROJECT_CONVERSATION_INSTRUCTION.lower()
    assert "creating or reusing sessions" in PROJECT_CONVERSATION_INSTRUCTION
    assert "do not create tickets to record a conversation" in PROJECT_CONVERSATION_INSTRUCTION
    assert "persistent work that moves through the project's stages" in (
        PROJECT_CONVERSATION_INSTRUCTION
    )
    assert "when the user asks to track or file the work" in PROJECT_CONVERSATION_INSTRUCTION
    assert "instead of creating a ticket speculatively" in PROJECT_CONVERSATION_INSTRUCTION
    assert "Conversation with the user creates no tickets on its own" in (
        PROJECT_ORCHESTRATOR_INSTRUCTION
    )
    assert "not to record a conversation" in SysTicketCreateTool.help_text


@pytest.mark.parametrize("projects_tools", [True, False])
async def test_mandate_is_labeled_and_ordered_before_ticket_guidance_and_role(projects_tools):
    spec = AgentSpec(
        spec_version=1,
        instructions="Shared agent instructions",
        tool_groups=ToolGroupsConfig(memory=False, projects=projects_tools),
    )
    snapshot = {
        "project_id": "project",
        "project_main_session": True,
        "project_mandate": "  Only edit files under docs/.\n\n## Style\nUse short sentences.  ",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        native = await _native_startup_instructions(spec, client, "session")
        sdk = await append_project_instructions("Author\n\nRequest", client, "session", spec)
    mandate = project_mandate_instruction(snapshot["project_mandate"])
    assert mandate is not None
    assert mandate.startswith("## Project mandate\n")
    assert "the project owner's instruction" in mandate
    assert "takes precedence over conflicting instructions" in mandate
    assert "never overrides safety rules, permission prompts or approval gates" in mandate
    assert "cannot widen it" in mandate
    assert mandate.endswith(
        "<project_mandate>\nOnly edit files under docs/.\n\n## Style\n"
        "Use short sentences.\n</project_mandate>"
    )
    for prompt, authored in ((native, spec.instructions), (sdk, "Author\n\nRequest")):
        assert prompt.startswith(authored)
        assert prompt.count(mandate) == 1
        if projects_tools:
            assert (
                prompt.index(mandate)
                < prompt.index(PROJECT_CONVERSATION_INSTRUCTION)
                < prompt.index(PROJECT_TICKET_WORK_INSTRUCTION)
                < prompt.index(PROJECT_ORCHESTRATOR_INSTRUCTION)
            )
        else:
            assert prompt.endswith(mandate)


async def test_lead_charter_follows_the_mandate_and_precedes_ticket_guidance_and_role():
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=True))
    snapshot = {
        "project_id": "project",
        "project_main_session": True,
        "project_mandate": "Stay in docs/.",
        "project_lead_charter": "  Batch reviews daily.  ",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        prompt = await append_project_instructions("Author", client, "session", spec)
    charter = project_lead_charter_instruction(snapshot["project_lead_charter"])
    assert charter is not None
    assert charter.endswith("<lead_charter>\nBatch reviews daily.\n</lead_charter>")
    assert "never widens it" in charter
    mandate = project_mandate_instruction("Stay in docs/.")
    assert mandate is not None
    assert (
        prompt.index(mandate)
        < prompt.index(charter)
        < prompt.index(PROJECT_CONVERSATION_INSTRUCTION)
        < prompt.index(PROJECT_ORCHESTRATOR_INSTRUCTION)
    )
    assert project_lead_charter_instruction("  ") is None


@pytest.mark.parametrize("mandate", [None, "", "   ", 7])
def test_absent_or_blank_mandate_adds_no_block(mandate):
    assert project_mandate_instruction(mandate) is None


async def test_agent_without_project_tools_gets_no_ticket_guidance_or_role():
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=False))
    snapshot = {"project_id": "project", "project_main_session": True}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        assert await append_project_instructions("Author", client, "session", spec) == "Author"
    assert await append_project_instructions("Author", None, "session", spec) == "Author"


async def test_unavailable_role_lookup_does_not_silently_start_an_ordinary_worker():
    spec = AgentSpec(spec_version=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        base_url="http://server.test",
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await append_project_instructions("Author", client, "session", spec)
