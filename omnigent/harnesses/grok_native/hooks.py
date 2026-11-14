"""Policy bridge for grok-native's ``x.ai/hooks`` client-registered hooks.

``grok agent serve`` lets an ACP client register hooks it runs at each
``pre_tool_use`` / ``stop`` / ``subagent_stop`` blocking event. This module
translates between Omnigent's policy verdicts and grok's hook decision
envelope so a PreToolUse-style event consults Omnigent's policy route (and
the permission-request route on ASK) before the tool runs.

See docs/GROK_NATIVE_DESIGN.md and ~/.grok/docs/user-guide/10-hooks.md for
the vendor's hook contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# grok hook decision → meaning for the tool call. The WS ``x.ai/hooks``
# extension advertises ``deny``/``block``; the config-layer hook schema also
# accepts ``allow``/``ask``/``defer`` decisions.
_HOOK_DECISIONS = ("allow", "deny", "ask", "defer")


@dataclass(frozen=True)
class HookDecision:
    """An Omnigent verdict translated into a grok hook reply.

    :param decision: ``allow`` / ``deny`` / ``ask`` / ``defer``.
    :param reason: Human-readable reason (deny reaches the model; ask labels
        the permission prompt).
    :param additional_context: Extra text fed to the model when set.
    """

    decision: str
    reason: str = ""
    additional_context: str = ""


def verdict_to_hook_decision(
    verdict: str,
    *,
    reason: str = "",
    additional_context: str = "",
) -> HookDecision:
    """Map an Omnigent policy verdict to a grok hook decision.

    ``ALLOW`` → ``allow``, ``DENY`` → ``deny``, ``ASK`` → ``ask`` (routes the
    call to Omnigent's elicitation via the permission prompt). Anything else
    defers so no opinion is stated.
    """
    decision = {
        "ALLOW": "allow",
        "DENY": "deny",
        "ASK": "ask",
    }.get(verdict.upper(), "defer")
    return HookDecision(decision=decision, reason=reason, additional_context=additional_context)


def hook_decision_payload(decision: HookDecision) -> dict[str, Any]:
    """Render a :class:`HookDecision` as the JSON reply grok expects.

    ``permissionDecision`` is the canonical field; ``reason`` rides
    ``permissionDecisionReason``. ``additionalContext`` is nested under
    ``hookSpecificOutput`` per the vendor hook doc.
    """
    if decision.decision not in _HOOK_DECISIONS:
        raise ValueError(f"unknown hook decision: {decision.decision!r}")
    payload: dict[str, Any] = {"hookSpecificOutput": {"permissionDecision": decision.decision}}
    if decision.reason:
        payload["hookSpecificOutput"]["permissionDecisionReason"] = decision.reason
    if decision.additional_context:
        payload["hookSpecificOutput"]["additionalContext"] = decision.additional_context
    return payload


def hook_event_tool_name(params: dict[str, Any]) -> str:
    """Pull the tool name out of a ``pre_tool_use`` hook event payload."""
    return str(params.get("toolName") or params.get("tool_name") or "")


def hook_event_tool_input(params: dict[str, Any]) -> dict[str, Any]:
    """Pull the tool input dict out of a ``pre_tool_use`` hook event payload."""
    raw = params.get("toolInput") or params.get("tool_input") or {}
    return raw if isinstance(raw, dict) else {}


def hook_event_session_id(params: dict[str, Any]) -> str:
    """Pull the session id out of a hook event payload."""
    return str(params.get("sessionId") or params.get("session_id") or "")


__all__ = [
    "HookDecision",
    "hook_decision_payload",
    "hook_event_session_id",
    "hook_event_tool_input",
    "hook_event_tool_name",
    "verdict_to_hook_decision",
]
