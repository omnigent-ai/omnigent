"""Sub-agent wake notices must not arrive as user-role messages.

Sub-agent wake notices must NOT be delivered as ``role="user"`` messages.
A safety-tuned Claude orchestrator sees:

    [user turn] "[System: sub-agent X finished … Call sys_read_inbox to collect.]"

and correctly identifies this as a prompt-injection attempt (a user message
claiming to be a system instruction), which causes it to refuse to call
``sys_read_inbox`` and strand the orchestration.

The fix must use a channel the model trusts as framework-originated
(``role="system"`` or a ``tool_result`` item) instead of ``role="user"``.

Two facets are covered:

1. ``_deliver_subagent_wake_post`` — the POST body sent by the runner to the
   server's ``/v1/sessions/{id}/events`` endpoint must carry a role that is NOT
   ``"user"``.  Currently it sends ``"role": "user"``; after the fix it should
   send ``"role": "system"`` (or an equivalent trusted channel).

2. ``_translate_input_to_messages`` (executor adapter) — when the server
   replays the stored wake item back to the executor, the ``Message`` that the
   executor receives must also carry a non-user role, so the ``_build_prompt``
   serializer does not render it as ``"user: [System: …]"``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app_mod
from omnigent.runtime.harnesses._executor_adapter import _translate_input_to_messages

# ─── helpers ──────────────────────────────────────────────────────────────────


class _CapturingClient:
    """HTTP client stub that records every POST body verbatim.

    A minimal real stub (not ``MagicMock``) so the wake POST path runs
    its actual logic and we can inspect what was sent.
    """

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: Any = None,
    ) -> httpx.Response:
        """Record ``json`` and return a synthetic 200."""
        self.bodies.append({"url": url, "json": json})
        request = httpx.Request("POST", f"http://test{url}")
        return httpx.Response(200, request=request, json={"id": "evt_1"})


# ─── facet 1: wake POST role ───────────────────────────────────────────────────


async def test_wake_post_body_role_is_not_user() -> None:
    """``_deliver_subagent_wake_post`` must send a role that is NOT ``"user"``.

    Previously the runner posted the wake notice with
    ``"role": "user"``, which caused Claude to treat it as a user-supplied
    instruction and refuse to act on it (prompt-injection detection).

    After the fix the role must be ``"system"`` (or another framework-trusted
    channel such as a ``tool_result`` item).  This test intentionally asserts
    the desired post-fix behaviour, so it **fails on the unfixed code** where
    ``role="user"`` is sent.
    """
    parent_id = "conv_parent_wake_role_repro"
    notice = runner_app_mod._format_subagent_wake_notice(
        agent="researcher",
        title="auth-study",
        status="completed",
        pending=1,
    )
    client = _CapturingClient()

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        notice,
    )

    assert delivered is True, "Wake POST should succeed with the mock client."
    assert len(client.bodies) == 1, f"Expected exactly one POST, got {len(client.bodies)}."

    body = client.bodies[0]["json"]
    assert body["type"] == "message", f"Expected type='message', got {body['type']!r}."

    role = body["data"]["role"]
    assert role != "user", (
        f"BUG: wake notice POSTed with role={role!r}. "
        "A safety-tuned Claude model sees '[System: …Call sys_read_inbox…]' "
        "arriving as a user-role message and flags it as prompt injection, "
        "causing the orchestrator to strand. "
        "Fix: use role='system' (or a tool_result channel) so the notice "
        "arrives on a framework-trusted channel."
    )
    # After the fix the role is expected to be "system".
    assert role == "system", (
        f"Expected role='system' for wake notice, got role={role!r}. "
        "The wake notice must arrive on the system channel so the model "
        "treats it as a framework instruction, not a user message."
    )


# ─── facet 2: executor message role ───────────────────────────────────────────


def test_wake_notice_executor_message_role_is_not_user() -> None:
    """The wake notice must not reach the executor as a ``role="user"`` message.

    When the server stores the wake item and the runner replays the conversation
    to the executor via ``_translate_input_to_messages``, the wake notice must
    arrive with ``role="system"`` (not ``"user"``), so ``_build_prompt`` does
    not render it as a user turn and no injection-detection fires.

    This test asserts the desired post-fix behaviour.  On unfixed code the
    final message in ``messages`` has ``role='user'``, making the test fail.
    """
    notice = runner_app_mod._format_subagent_wake_notice(
        agent="researcher",
        title="auth-study",
        status="completed",
        pending=1,
    )

    # Simulate the conversation history the runner sends to the executor after
    # a sub-agent wake: user dispatch request → assistant ack → wake notice.
    simulated_input = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Dispatch the researcher."}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Dispatching researcher sub-agent."}],
        },
        {
            # After the fix this item should be stored with role="system", not "user".
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": notice}],
        },
    ]

    messages = _translate_input_to_messages(simulated_input)

    assert messages, "Translator must produce at least one message."

    # The wake notice is the last message in the history.
    wake_msg = messages[-1]
    role = wake_msg.get("role")
    content = wake_msg.get("content", "")
    content_str = content if isinstance(content, str) else str(content)

    assert "[System:" in content_str, (
        f"Last message does not look like a wake notice: {content_str!r}"
    )
    assert role != "user", (
        f"BUG: wake notice reached the executor as role={role!r}. "
        "Claude's _build_prompt serializes this as 'user: [System: …]', which "
        "triggers prompt-injection detection and causes the orchestrator to "
        "refuse sys_read_inbox. Fix: store and replay with role='system'."
    )
    assert role == "system", (
        f"Expected role='system' for wake notice at executor boundary, "
        f"got role={role!r}."
    )
