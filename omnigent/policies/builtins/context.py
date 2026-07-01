"""Built-in context-management policies.

Helps agents keep their working context lean by enforcing limits on
conversation depth.  The guiding principle: the goal is not fewer
tokens *used*, but fewer tokens *wasted* — sprawling context filled
with stale tool results from a prior task degrades quality without
adding value.

The recommended response to a denial is to start a fresh session for
the new task rather than compacting or summarising in place.
"""

from __future__ import annotations

import logging
from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_log = logging.getLogger(__name__)


def cap_conversation_depth(
    *,
    max_messages: int,
    action: str = "DENY",
) -> PolicyCallable:
    """Factory: deny or ask when conversation context grows too deep.

    Fires on ``llm_request`` events.  When the number of messages in
    the current context window exceeds *max_messages*, the policy
    returns *action* with a recommendation to start a fresh session
    instead of continuing in an over-loaded context.

    The intent is to keep context lean by catching the moment where
    accumulated history from a prior task would degrade the quality of
    the next one.  Compacting or summarising is explicitly *not* the
    recommended remedy — starting a new session is.

    :param max_messages: Maximum number of messages allowed in a
        single context before the policy fires.  Counts every entry
        in the conversation (system, user, assistant, tool) as
        reported by the ``messages_count`` field of the
        ``llm_request`` event.
    :param action: Response to return when the threshold is crossed.
        ``"DENY"`` (default) blocks the LLM call and surfaces the
        message to the agent; ``"ASK"`` escalates to the user for
        approval.  Any other value is treated as ``"DENY"``.
    :returns: An async-compatible policy callable that fires on
        ``llm_request`` events.
    """
    normalised_action = action.upper() if isinstance(action, str) else "DENY"
    if normalised_action not in {"DENY", "ASK"}:
        _log.warning(
            "cap_conversation_depth: unknown action %r — defaulting to DENY",
            action,
        )
        normalised_action = "DENY"

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """Block or escalate when context depth exceeds the configured limit.

        :param event: Policy event dict.
        :returns: A DENY or ASK response when the limit is exceeded;
            ``None`` (abstain) otherwise.
        """
        if event.get("type") != "llm_request":
            return None

        data = event.get("data")
        if not isinstance(data, dict):
            return None

        messages_count: Any = data.get("messages_count", 0)
        try:
            count = int(messages_count)
        except (TypeError, ValueError):
            return None

        if count <= max_messages:
            return None

        _log.info(
            "cap_conversation_depth: messages_count=%d exceeds max=%d — action=%s",
            count,
            max_messages,
            normalised_action,
        )
        return {
            "result": normalised_action,
            "reason": (
                f"This conversation has grown to {count} messages "
                f"(limit: {max_messages}). "
                "Long contexts accumulate stale history from prior tasks, "
                "which wastes capacity without adding value. "
                "Start a fresh session for your next task rather than "
                "continuing here."
            ),
        }

    return evaluate  # type: ignore[return-value]


# ── Registry ──────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.context.cap_conversation_depth",
        "kind": "factory",
        "name": "Cap Conversation Depth",
        "description": (
            "Denies (or asks) when the number of messages in the current "
            "context exceeds a configured limit. Encourages agents to start "
            "fresh sessions when switching tasks rather than accumulating "
            "stale context — the goal is fewer tokens wasted, not just fewer "
            "tokens used."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "max_messages": {
                    "type": "integer",
                    "description": (
                        "Maximum number of messages allowed in the context "
                        "before the policy fires. Counts all entries "
                        "(system, user, assistant, tool results)."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["DENY", "ASK"],
                    "default": "DENY",
                    "description": (
                        "Response when the threshold is crossed. "
                        "DENY blocks the LLM call; ASK escalates to the user."
                    ),
                },
            },
            "required": ["max_messages"],
        },
    },
]
