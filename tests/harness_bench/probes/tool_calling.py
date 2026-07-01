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

from tests.harness_bench.driver import SdkInprocDriver
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict

_TOOL_NAME = "bench_echo"
_TOOL_SPEC = [
    {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": "Echo a token back. Call this to retrieve the secret token.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "what to echo"}},
                "required": ["query"],
            },
        },
    }
]


class ToolCallingProbe(CapabilityProbe):
    name = "tool_calling"
    title = "Tool calling"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: SdkInprocDriver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_turn(
            f"You must call the {_TOOL_NAME} tool with query='token' to get the secret, "
            "then reply with the tool's output verbatim.",
            tools=_TOOL_SPEC,
            auto_tool_output=f"the-secret-is-{profile.marker}",
            timeout=150.0,
        )
        called = [tc for tc in result.tool_calls if tc.get("name") == _TOOL_NAME]
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
