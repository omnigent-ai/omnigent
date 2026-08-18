"""ACP PreToolUse hook for gating tools via Omnigent policy evaluation.

This module is invoked as a subprocess by Devin (or any ACP agent) when a tool
use event occurs. It reads the hook payload from stdin, evaluates the tool call
against Omnigent's policy engine, and returns a block/allow decision on stdout.

The hook format is identical to Claude Code's native hooks, so agents reading
Claude Code hook configs will see `PreToolUse` events here. MCP tools (tool_name
matching `^mcp__.*`) are included in the gate since they are loaded and run by
the agent itself, not by the omnigent relay.

Failure handling: if the policy evaluation endpoint is unreachable or returns
an error, the hook fails closed by denying the tool call (consistent with the
runner-side default).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from omnigent.native_policy_hook import (
    evaluation_response_to_hook_output,
    fail_closed_hook_output,
    hook_payload_to_evaluation_request,
    post_evaluate_with_retry,
    read_relay_policy_config,
    relay_policy_evaluate_url,
)

_logger = logging.getLogger(__name__)

# Hook event type we gate at. PostToolUse and UserPromptSubmit are not handled
# here (ACP agents don't have UserPromptSubmit since they manage their own loop).
_PRE_TOOL_USE = "PreToolUse"

# Env var for the relay directory (also accepted via ``--relay-dir``).
_RELAY_DIR_ENV = "_OMNIGENT_RELAY_DIR"

# Read timeout for the policy evaluate POST. Long, because the evaluate endpoint
# holds the gate open for server-side ASK elicitation (URL-based), mirroring the
# native-hook timeout.
_EVALUATE_READ_TIMEOUT_S = 86400.0


def main(argv: list[str] | None = None) -> int:
    """
    Evaluate an ACP ``PreToolUse`` hook against Omnigent policies.

    Reads the hook payload from stdin (JSON), converts it to an
    ``EvaluationRequest``, POSTs to the policy evaluate endpoint, and writes
    the decision on stdout.

    :param argv: Optional argv override (excluding program name). ``None`` reads
        :data:`sys.argv`.
    :returns: Process exit code (always 0 — blocking verdicts are expressed
        via JSON output, not exit codes).
    """
    raw_argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Evaluate ACP tool use against Omnigent policy")
    parser.add_argument(
        "--relay-dir",
        type=str,
        help="Path to relay directory containing tool relay config",
    )
    args = parser.parse_args(raw_argv)

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # Malformed input: fail silently (no policy-governed session).
        _logger.warning("acp hook: failed to parse stdin JSON: %s", exc)
        return 0

    hook_event = payload.get("hook_event_name", "")
    if hook_event != _PRE_TOOL_USE:
        # Only handle PreToolUse; other events are not policy-gated here.
        return 0

    # Convert to evaluation request. Skips mcp__omnigent__* tools but includes
    # all mcp__* tools the agent loaded itself.
    eval_request = hook_payload_to_evaluation_request(hook_event, payload)
    if eval_request is None:
        # Not a policy-relevant event (e.g., an omnigent MCP tool).
        return 0

    # Resolve the policy endpoint. The executor writes relay config when
    # gating is enabled; if absent, the session is not governed.
    relay_dir = args.relay_dir or os.environ.get(_RELAY_DIR_ENV)
    if not relay_dir:
        return 0

    relay_config = read_relay_policy_config(Path(relay_dir))
    if relay_config is None:
        return 0

    relay_url, relay_token, _session_id = relay_config
    evaluate_url = relay_policy_evaluate_url(relay_url)
    # The relay authenticates with the token from the relay config (the same
    # shape the native hooks use); no per-request reauth on the relay path.
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {relay_token}",
    }

    def _fail_closed() -> int:
        output = fail_closed_hook_output(hook_event)
        if output is not None:
            json.dump(output, sys.stdout)
        return 0

    resp, api_error = post_evaluate_with_retry(
        evaluate_url,
        headers,
        eval_request,
        _EVALUATE_READ_TIMEOUT_S,
        "acp tool-use",
        reauth=None,
    )
    if resp is None:
        # Endpoint unreachable or non-2xx after retries — fail closed (deny).
        _logger.warning("acp hook: policy evaluate failed (%s); failing closed", api_error)
        return _fail_closed()
    if not resp.content:
        _logger.warning("acp hook: empty policy response; failing closed")
        return _fail_closed()
    try:
        eval_response = resp.json()
    except (json.JSONDecodeError, ValueError):
        _logger.warning("acp hook: malformed policy response; failing closed")
        return _fail_closed()

    # Convert the evaluation response to hook output (deny/allow decision).
    output = evaluation_response_to_hook_output(hook_event, eval_response)
    if output is not None:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
