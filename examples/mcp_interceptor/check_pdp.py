#!/usr/bin/env python3
"""Smoke-test the mcp_interceptor policy against a live MCP Interceptor PDP (no LLM).

Imports the real :func:`omnigent.policies.builtins.mcp_interceptor.mcp_interceptor`
policy, feeds it a ``tool_call`` event shaped exactly as omnigent's policy engine
does, and prints the verdict returned by the LIVE PDP. Use this to confirm your
endpoint + token + policy wiring before running the full agent (config.yaml).

Usage::

    export MCP_INTERCEPTOR_TOKEN=<your-pdp-bearer-token>

    python examples/mcp_interceptor/check_pdp.py \
        --endpoint https://your-pdp.example.com/api/interceptor --tool send_email

Exit status is non-zero if the PDP did not return an explicit verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from omnigent.policies.builtins.mcp_interceptor import mcp_interceptor

DEFAULT_ENDPOINT = "https://your-pdp.example.com/api/interceptor"


def _tool_call_event(tool: str, arguments: dict) -> dict:
    """A tool_call PolicyEvent, matching omnigent.policies.function._build_event."""
    return {
        "type": "tool_call",
        "target": tool,
        "data": {"name": tool, "arguments": arguments},
        "context": {"actor": {"run_as": "demo@example.com"}, "usage": {}},
        "session_state": {},
    }


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--endpoint",
        default=os.environ.get("MCP_INTERCEPTOR_ENDPOINT", DEFAULT_ENDPOINT),
        help="MCP Interceptor PDP URL accepting the JSON-RPC POST.",
    )
    ap.add_argument("--tool", default="send_email", help="Tool name to validate.")
    args = ap.parse_args()

    token = os.environ.get("MCP_INTERCEPTOR_TOKEN")
    if not token:
        raise SystemExit("Set MCP_INTERCEPTOR_TOKEN to a PDP bearer token.")

    policy = mcp_interceptor(endpoint=args.endpoint, api_key=token, on_notify="deny", timeout_s=20)
    verdict = await policy(_tool_call_event(args.tool, {"to": "x@example.com", "body": "hi"}))

    print(json.dumps(verdict, indent=2))
    result = verdict.get("result") if verdict else None
    print(f"\nverdict: {result or 'ALLOW (policy abstained)'}")
    return 0 if result in {"ALLOW", "DENY", "ASK"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
