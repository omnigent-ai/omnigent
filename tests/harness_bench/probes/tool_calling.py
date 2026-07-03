"""Tool-calling probe — can the harness call a server-dispatched tool?

Offers one function tool, asks the model to call it, and auto-returns a
result so the turn can complete. Observing a ``response.tool_call`` proves
the harness surfaces tool calls to the server for dispatch; the turn
completing after the result is delivered proves the round-trip closes.

This is the "can it use a tool at all" signal. The finer "Connects to
Omnigent MCP" column (MCP transport vs a non-MCP tool bridge) is a
separate phase-2 dimension; every P0 SDK harness calls tools, only the
transport differs.
"""

from __future__ import annotations

from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class ToolCallingProbe(CapabilityProbe):
    name = "tool_calling"
    title = "Tool calling"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        # The driver owns the tool mechanism (a request-level function tool on
        # the wrap path, a builtin on full-server); the probe only cares that
        # *a* tool call was dispatched and the turn closed.
        result = await driver.run_tool_turn(deny=False)
        called = list(result.tool_calls)
        detail = {
            "tool_calls": [tc.get("name") for tc in result.tool_calls],
            "completed": result.completed,
        }
        if result.timed_out and not called:
            return ProbeResult(
                Verdict.SKIPPED, note="timed out before any tool call", detail=detail
            )
        if not called:
            # The turn ran without dispatching the offered tool. This is
            # ambiguous: some harnesses (codex, openai-agents) accept a
            # request-level tool and surface a server-dispatched call, while
            # others (claude-sdk, pi) register tools via agent config / MCP
            # and ignore the wire-level `tools` field. Not calling it here is
            # therefore not proof the harness cannot call tools — report
            # SKIPPED rather than a false UNSUPPORTED.
            return ProbeResult(
                Verdict.SKIPPED,
                note=(
                    "offered tool not dispatched "
                    "(harness may register tools via config/MCP, not the request)"
                ),
                detail=detail,
            )
        if result.completed:
            return ProbeResult(
                Verdict.SUPPORTED,
                note="tool call dispatched; result delivered; turn completed",
                detail=detail,
            )
        # The tool call surfaced (the load-bearing signal) but the turn did
        # not cleanly complete after the result — partial support.
        return ProbeResult(
            Verdict.PARTIAL,
            note="tool call surfaced but turn did not complete after result",
            detail=detail,
        )
