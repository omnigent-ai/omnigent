"""Credential-free workflow demonstration."""

import asyncio
import json

from omnigent.workflows.engine import MemberRequest
from omnigent.workflows.gates import GateRequest
from omnigent.workflows.state import GateOutcome


async def simulate_member(request: MemberRequest) -> str:
    """Exercise orchestration without launching agents or modifying a workspace."""
    request.record.session_id = request.record.session_id or (
        f"sim-{request.run_id}-{request.node.id}-{request.member.id}"
    )
    request.checkpoint()
    await asyncio.sleep(0)
    if request.phase == "contribute":
        return f"SIMULATED {request.member.role}: {request.node.goal}"
    additions = []
    if request.node.allow_expansion:
        additions.append(
            {
                "id": request.node.id[:55] + "_check",
                "goal": f"Verify the findings from {request.node.id}",
                "team": request.node.team,
            }
        )
    return json.dumps(
        {
            "summary": f"SIMULATED team result: {request.node.goal}",
            "outputs": {"result": {"task": request.node.id, "revision": f"sim-{request.round}"}},
            "add_nodes": additions,
            "artifacts": [
                {
                    "id": "result",
                    "kind": "report",
                    "uri": f"simulation://{request.node.id}/result",
                    "revision": f"sim-{request.round}",
                }
            ],
        }
    )


async def simulate_gate(request: GateRequest) -> GateOutcome:
    """Simulation never executes commands from a workflow definition."""
    return GateOutcome(passed=True, detail=f"SIMULATED check: {request.gate.id}")
