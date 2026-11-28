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
from omnigent.runtime.side_chat_instructions import side_chat_instruction
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


@pytest.mark.parametrize("projects", [True, False])
async def test_side_chat_of_any_session_is_told_to_hand_back_to_its_main_session(projects):
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=projects))
    snapshot = {"side_chat_parent_id": "main-session"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        prompt = await append_project_instructions("Author", client, "session", spec)
    note = side_chat_instruction("main-session")
    assert note is not None
    assert prompt == f"Author\n\n{note}"
    assert 'sys_session_message (session_id="main-session")' in note
    assert "never interrupts" in note
    assert PROJECT_ORCHESTRATOR_INSTRUCTION not in prompt


@pytest.mark.parametrize("parent", [None, "", "  ", 3])
def test_ordinary_session_gets_no_side_chat_note(parent):
    assert side_chat_instruction(parent) is None


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


def test_worker_turn_says_when_a_comment_cleared_needs_human():
    from omnigent.server.project_orchestrator import ticket_comment_for_worker_message

    event = {"ticket_id": "t1", "ticket_title": "Decide", "payload": {"text": "B"}}
    assert "cleared" not in ticket_comment_for_worker_message("p1", event)
    event["payload"]["cleared_needs_human"] = True
    assert "add needs-human back" in ticket_comment_for_worker_message("p1", event)


def test_worker_turn_on_a_completed_ticket_says_how_to_reopen_or_follow_up():
    from omnigent.server.project_orchestrator import ticket_comment_for_worker_message

    event = {"ticket_id": "t1", "ticket_title": "502s", "payload": {"text": "Still broken"}}
    assert "completed stage" not in ticket_comment_for_worker_message("p1", event)
    text = ticket_comment_for_worker_message("p1", event, completed=True)
    assert "completed stage" in text
    assert "sys_ticket_update" in text and "sys_ticket_link" in text
    assert "thank-you" in text


def test_sign_off_turns_name_the_label_and_the_approved_commit():
    from omnigent.server.project_orchestrator import (
        ticket_gate_label_for_merger_message,
        ticket_gate_label_for_worker_message,
    )

    event = {
        "ticket_id": "t1",
        "ticket_title": "Risky",
        "payload": {"gate_labels_added": ["risk-ok"]},
    }
    assert "added `risk-ok`" in ticket_gate_label_for_worker_message("p1", event)
    assert "cleared" not in ticket_gate_label_for_worker_message("p1", event)
    event["payload"]["answered_ask"] = True
    assert "add needs-human back" in ticket_gate_label_for_worker_message("p1", event)
    assert "`" + "b" * 40 + "`" in ticket_gate_label_for_merger_message("p1", event, "b" * 40)


async def test_a_running_session_gets_changed_project_text_once_on_its_next_turn():
    from omnigent.runner.project_orchestrator import (
        PROJECT_INSTRUCTIONS_UPDATED_NOTICE,
        project_instructions_update,
    )

    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=True))
    snapshot = {"project_id": "project", "project_mandate": "Old rules."}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        assert await project_instructions_update(client, "running") is None  # never launched
        await append_project_instructions("Author", client, "running", spec)
        assert await project_instructions_update(client, "running") is None  # unchanged
        snapshot["project_mandate"] = "New rules."
        update = await project_instructions_update(client, "running")
        assert update is not None and update.startswith(PROJECT_INSTRUCTIONS_UPDATED_NOTICE)
        assert "New rules." in update and "Old rules." not in update
        assert await project_instructions_update(client, "running") is None  # delivered once


async def test_a_session_this_runner_never_launched_gets_its_text_once_after_a_restart():
    from omnigent.runner.project_orchestrator import (
        PROJECT_INSTRUCTIONS_UPDATED_NOTICE,
        project_instructions_update,
    )

    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=True))
    snapshot = {"project_id": "project", "project_mandate": "Current rules."}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        # The runner restarted: no record of what this running session was given.
        update = await project_instructions_update(client, "resumed", spec)
        assert update is not None and update.startswith(PROJECT_INSTRUCTIONS_UPDATED_NOTICE)
        assert "Current rules." in update
        assert await project_instructions_update(client, "resumed", spec) is None
        snapshot["project_mandate"] = "Newer rules."
        later = await project_instructions_update(client, "resumed", spec)
        assert later is not None and "Newer rules." in later


async def test_only_native_launch_text_says_updates_may_arrive_in_a_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    from omnigent.runtime.project_instructions_notice import session_nonce
    from omnigent.spec.types import ExecutorSpec

    snapshot = {"project_id": "project", "project_mandate": "Rules."}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=snapshot)),
        base_url="http://server.test",
    ) as client:
        native = AgentSpec(
            spec_version=1,
            tool_groups=ToolGroupsConfig(projects=True),
            executor=ExecutorSpec(config={"harness": "codex-native"}),
        )
        sdk = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=True))
        native_text = await append_project_instructions("Author", client, "native", native)
        sdk_text = await append_project_instructions("Author", client, "sdk", sdk)
    assert native_text is not None and f'nonce="{session_nonce("native")}"' in native_text
    assert sdk_text is not None and "nonce=" not in sdk_text


async def test_an_unreadable_snapshot_skips_the_update_notice_not_the_turn():
    from omnigent.runner.project_orchestrator import project_instructions_update

    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=True))
    responses = [httpx.Response(200, json={"project_id": "p", "project_mandate": "M."})]

    def respond(_: httpx.Request) -> httpx.Response:
        return responses.pop(0) if responses else httpx.Response(503)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server.test"
    ) as client:
        await append_project_instructions("Author", client, "flaky", spec)
        assert await project_instructions_update(client, "flaky") is None


def test_project_identity_block_leaves_out_the_role_scope() -> None:
    from omnigent.runner.project_orchestrator import project_identity_instruction

    # An older server may still send a scope; the role's instructions never carry it.
    block = project_identity_instruction(
        {"name": "Designer", "instructions": "Design.", "scope": "Owns: UX\nDoesn't own: roadmap"}
    )
    assert block is not None
    assert "<project_identity_persona>\nDesign.\n</project_identity_persona>" in block
    assert "<project_identity_scope>" not in block
    assert "Owns: UX" not in block
