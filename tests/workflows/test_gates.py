"""Command gate subprocess boundaries: candidate input, output, and owned cleanup."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from omnigent.workflows import CommandGate, NodeResult, Workflow
from omnigent.workflows.gates import GateRequest, execute_command_gate
from tests._helpers.polling import poll_until_async


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_command_receives_candidate_and_retains_bounded_output(
    workflow: Workflow, tmp_path: Path, exit_code: int
) -> None:
    gate = CommandGate(
        id="tests",
        cwd=str(tmp_path),
        command=(
            sys.executable,
            "-c",
            "import json,sys; candidate=json.load(sys.stdin); "
            "print('x'*20000); print(candidate['candidate_id'], candidate['summary']); "
            f"sys.exit({exit_code})",
        ),
    )
    result = await execute_command_gate(
        GateRequest(workflow.nodes[0], gate, "root-1", NodeResult(summary="ready"))
    )
    assert result.passed == (exit_code == 0)
    assert result.detail.endswith("root-1 ready\n")
    assert len(result.detail) < 16500


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_command_is_reaped_on_timeout_or_cancellation(
    workflow: Workflow, tmp_path: Path, stop: str
) -> None:
    pid_path = tmp_path / "pid"
    gate = CommandGate(
        id="tests",
        cwd=str(tmp_path),
        timeout_seconds=1 if stop == "timeout" else 30,
        command=(
            sys.executable,
            "-c",
            "import os,pathlib,time; pathlib.Path('pid').write_text(str(os.getpid())); "
            "time.sleep(60)",
        ),
    )
    task = asyncio.create_task(
        execute_command_gate(
            GateRequest(workflow.nodes[0], gate, "root-1", NodeResult(summary="ready"))
        )
    )
    await poll_until_async(pid_path.exists, timeout_s=5, interval_s=0.01)
    pid = int(pid_path.read_text())
    if stop == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert not result.passed and "Timed out" in result.detail
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
