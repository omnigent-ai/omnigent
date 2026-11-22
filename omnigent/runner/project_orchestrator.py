"""Deliver project ticket guidance without changing an agent's authored instructions."""

from __future__ import annotations

import httpx

from omnigent.runtime.project_ticket_instructions import PROJECT_TICKET_WORK_INSTRUCTION
from omnigent.runtime.prompt import append_framework_instructions
from omnigent.spec import AgentSpec

PROJECT_ORCHESTRATOR_INSTRUCTION = (
    "You are this project's orchestrator. Your primary responsibility is forward progress "
    "on every unfinished ticket within the user's authorized scope. Before responding to a "
    "request or ticket update, load and follow the orchestrator skill. "
    "If no genuine human decision is needed, arrange the next authorized action; do not park "
    "work after a plan or worker reply. Advance stages as the actual work changes, not to "
    "simulate progress. Use one recurring check about every two minutes to read all pages "
    "of the board and account for every unfinished ticket and unresolved human request, "
    "including idle or unassigned work. Before declaring the board waiting on the human or "
    "stopping checks, verify every unfinished ticket has an explicit user pause/scope limit "
    "or a substantiated human blocker: the blocked action, exact question, needs-human label "
    "and a link to the responsible worker's chat. Keep monitoring running work and external "
    "dependencies; they are not human blockers. Resolve routine choices independently; "
    "a finished plan creates no new approval requirement. Respect explicit planning-only "
    "scope, Stop and actual approval gates. "
    "Dispatch independent ready work in parallel where capacity and isolated ownership allow; "
    "do not wait for a worker's startup or reply before dispatching unrelated work. "
    "A queued task is accepted, not proof the worker is ready; monitor startup failures. "
    "Read the project's current stage definitions with sys_ticket_list or sys_ticket_get; "
    "use their stable stage IDs, instructions and completion flags, not fixed stage names. "
    "Stage instructions and stage changes do not grant approval or bypass permission prompts. "
    "You MUST remain available for incoming messages: NEVER initiate a human elicitation "
    "in this session (including AskUserQuestion, request_user_input, ExitPlanMode, "
    "sys_workflow_request_input, or equivalents). Delegate human questions and approval "
    "prompts to the linked worker handling the ticket; never synchronously wait for its "
    "human answer. Ask any coordinator-only question in ordinary chat and yield. "
    "This restriction does not authorize bypassing mandatory permission or approval gates."
)


async def project_orchestrator_instructions(
    server_client: httpx.AsyncClient | None,
    session_id: str,
    spec: AgentSpec | None,
) -> str | None:
    """Resolve ticket guidance and the role from the session, never its agent or title."""
    if server_client is None or spec is None or not spec.tool_groups.projects:
        return None
    response = await server_client.get(
        f"/v1/sessions/{session_id}",
        params={
            "include_items": "false",
            "include_liveness": "false",
            "include_live_status": "false",
            "include_usage": "false",
        },
        timeout=5.0,
    )
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        response.raise_for_status()
    snapshot = response.json()
    if not isinstance(snapshot, dict):
        return None
    instructions = []
    if snapshot.get("project_id"):
        instructions.append(PROJECT_TICKET_WORK_INSTRUCTION)
    # Legacy server response fallback; remove in 0.25.0.
    if snapshot.get("project_main_session", snapshot.get("project_orchestrator")) is True:
        instructions.append(PROJECT_ORCHESTRATOR_INSTRUCTION)
    return "\n\n".join(instructions) or None


async def append_project_orchestrator_instructions(
    instructions: str | None,
    server_client: httpx.AsyncClient | None,
    session_id: str,
    spec: AgentSpec | None,
) -> str | None:
    """Use the shared composer for native startup and per-turn SDK instructions."""
    role = await project_orchestrator_instructions(server_client, session_id, spec)
    return append_framework_instructions(instructions, (role,) if role else ())
