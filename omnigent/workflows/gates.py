"""Execute configured checks against a candidate, independently of the owner."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from dataclasses import dataclass

from omnigent.workflows.models import CommandGate, NodeResult, WorkflowNode
from omnigent.workflows.state import GateOutcome


@dataclass(frozen=True)
class GateRequest:
    node: WorkflowNode
    gate: CommandGate
    candidate_id: str
    candidate: NodeResult


async def execute_command_gate(request: GateRequest) -> GateOutcome:
    """Send candidate JSON on stdin; retain a bounded output tail and reap on cancellation."""
    gate = request.gate
    payload = json.dumps(
        {"candidate_id": request.candidate_id, **request.candidate.model_dump(mode="json")}
    ).encode()
    with tempfile.TemporaryFile() as candidate_input, tempfile.TemporaryFile() as output:
        candidate_input.write(payload)
        candidate_input.seek(0)
        process = await asyncio.create_subprocess_exec(
            *gate.command,
            cwd=gate.cwd,
            stdin=candidate_input,
            stdout=output,
            stderr=output,
            start_new_session=os.name == "posix",
        )
        timed_out = False
        try:
            async with asyncio.timeout(gate.timeout_seconds):
                await process.wait()
        except TimeoutError:
            timed_out = True
        finally:
            if process.returncode is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        output.seek(max(0, output.tell() - 16384))
        tail = output.read().decode(errors="replace")
    return GateOutcome(
        passed=not timed_out and process.returncode == 0,
        detail=f"{'Timed out' if timed_out else f'Exit {process.returncode}'}\n{tail}",
    )
