"""
E2E regression test: an agent-declared output DENY policy must be
enforced on the claude-sdk harness, not silently advisory.

Journey (mirrors the bug report's "Steps to reproduce" through the real
server + runner topology the e2e suite stands up):

1. Register an agent whose ``policies:`` block declares an output-phase
   DENY policy (fires on ``response`` / ``llm_response`` events).
2. Create a runner-bound session for it and send one user message.
3. The (mock) LLM replies with a marker text the policy denies.
4. Expected: the DENY is enforced — the denied assistant text is never
   persisted as a normal assistant message (this test asserts on the
   durable record via the terminal session snapshot; the transient
   live-stream flash of the text before the verdict lands is a separate,
   buffering-scoped gap it does not cover).
   Actual (bug): the marker text lands in ``conversation_items``
   unmodified; the DENY verdict only produces a trailing error item
   *after* the content was already delivered.

The policy uses the registered built-in factory
``omnigent.policies.function.make_fixed_action_callable`` with
``on_phases: [response, llm_response]`` so it abstains on every other
phase and returns DENY for the assistant's output — the same shape as
the reporter's ``tripwire_policy.deny_on_output`` callable, without
needing a custom ``policy_modules`` entry.

What breaks if this fails:

- ``ClaudeSDKExecutor``'s ``PHASE_LLM_RESPONSE`` gate stops preventing
  denied content from reaching the user (the reported bug), or
- the policy engine stops evaluating output phases for claude-sdk
  sessions entirely (the DENY evidence assert catches that).

Usage::

    pytest tests/e2e/test_output_policy_deny_enforced_claude_sdk.py -v
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)

# The marker the mock LLM emits as the assistant's reply. The output
# policy denies the whole assistant turn, so this exact text must never
# persist as durable assistant output. Deliberately unlikely to appear
# in incidental output.
_MARKER = "TRIPWIRE_DENIED_OUTPUT_MARKER"

# Substring of the deny reason / executor error, used as evidence the
# policy verdict actually fired (guards against a vacuous pass where the
# turn failed before the policy could ever run).
_DENY_EVIDENCE = "denied by policy"

_TURN_TIMEOUT_S = 240


def _assistant_texts(body: dict[str, Any]) -> list[str]:
    """Collect assistant-message text blocks from a terminal response body."""
    texts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") != "message":
            continue
        if item.get("role") not in (None, "assistant"):
            continue
        for block in item.get("content", []):
            if isinstance(block, dict):
                text = block.get("text") or block.get("output_text")
                if isinstance(text, str):
                    texts.append(text)
    return texts


def _all_output_text(body: dict[str, Any]) -> str:
    """Flatten every output item (messages, errors, tool calls) to one string."""
    import json as _json

    return _json.dumps(body.get("output", [])) + " " + str(body.get("error") or "")


def test_output_policy_deny_is_enforced_on_claude_sdk(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    An agent-declared output DENY policy must keep the denied assistant
    text out of the durable transcript on the claude-sdk harness.

    While the bug is live this fails: the denied marker text is
    persisted as a normal assistant message even though the policy
    returned DENY for the turn. (The transient live-stream flash before
    the verdict lands is out of this test's scope.)
    """
    skip_if_harness_cli_missing("claude-sdk")

    model = f"mock-outpol-{uuid.uuid4().hex[:8]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"outpol-{uuid.uuid4().hex[:8]}",
        harness="claude-sdk",
        model=model,
        profile="",
        prompt="Answer exactly what you are asked, in one line.",
        mock_llm_base_url=mock_llm_server_url,
        extra_config={
            "policies": {
                "deny_all_output": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.policies.function.make_fixed_action_callable",
                        "arguments": {
                            "action": "deny",
                            "reason": "output denied by policy (marker tripwire)",
                            "on_phases": ["response", "llm_response"],
                        },
                    },
                },
            },
        },
    )

    # Queue several copies: background calls for the same session (e.g.
    # title generation) share the model-keyed queue and drain entries
    # ahead of the actual turn.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _MARKER} for _ in range(6)],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=f"Reply with exactly: {_MARKER}",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=_TURN_TIMEOUT_S,
    )

    # Guard against a vacuous pass: the output policy must actually have
    # fired. On both the buggy and the enforced path the DENY leaves a
    # trace — an executor error / deny sentinel mentioning the policy.
    # If neither is present the turn never reached the output gate
    # (harness boot failure, policy not attached) and the test must not
    # silently pass.
    flat = _all_output_text(body).lower()
    assert _DENY_EVIDENCE in flat, (
        "expected evidence that the output policy evaluated and denied the "
        f"turn (substring {_DENY_EVIDENCE!r}); got output: {flat[:1500]}"
    )

    # THE regression assert: the denied assistant text must not be
    # delivered/persisted as a normal assistant message. While the bug
    # is live, the marker text streams to the user and is stored in
    # conversation_items unmodified, so this fails.
    delivered = [t for t in _assistant_texts(body) if _MARKER in t]
    assert not delivered, (
        "output policy returned DENY for the assistant turn, but the denied "
        f"text was still delivered/persisted as assistant output: {delivered!r}. "
        "LLM_RESPONSE DENY is not enforced on claude-sdk — the "
        "content reaches the user before (or despite) the policy verdict."
    )
