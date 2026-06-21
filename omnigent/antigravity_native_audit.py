"""Post-hoc tool-call policy audit for the native Antigravity (agy) forwarder.

**This is POST-HOC, NOT a blocking gate.** agy writes a transcript step only at
``DONE`` (after the tool has executed; there is no token streaming and no
pre-execution signal), and its ``hooks.json`` ``PreToolUse`` hook does **not**
fire on tool execution in agy 1.0.8 (verified — see
``docs/claude/antigravity-native-governance-design.md`` §2.3). So unlike
claude-native's ``PreToolUse`` / codex-native's trusted hook, this harness
**cannot intercept a tool before it runs**. The only honest Omnigent policy
enforcement here is to *observe* each tool call as it appears in the transcript,
evaluate it against the session's policies, and — on a DENY/ASK — surface a
warning conversation item (and, optionally, best-effort interrupt the in-flight
turn; see :func:`maybe_interrupt_turn`). The offending tool has already run by
the time the warning lands.

This module owns the pure, unit-testable pieces of that flow:

* :func:`step_to_audit_tool_calls` — extract neutral ``{tool_name, tool_input}``
  records from one agy transcript step (the ``PLANNER_RESPONSE`` tool-call step).
* :func:`build_audit_evaluation_request` — wrap a tool call as the proto
  ``EvaluationRequest`` the server's ``POST /policies/evaluate`` consumes,
  reusing :func:`omnigent.native_policy_hook.hook_payload_to_evaluation_request`
  and stamping ``context.harness = "antigravity-native"`` + ``context.model``.
* :func:`audit_verdict_is_violation` / :func:`audit_violation_warning_text` —
  classify a verdict and render the warning text.
* :func:`build_policy_violation_item` / :func:`build_degrade_notice_item` — the
  two conversation items the forwarder POSTs (the per-violation warning and the
  one-time audit-only degrade notice).

The async POST + the OFF-by-default interrupt live in
:mod:`omnigent.antigravity_native_forwarder` (they need the live Omnigent client
and the connect-RPC port); they delegate the classification/rendering here.

**Phase used: ``PHASE_TOOL_CALL``.** Tool-name / cost / CEL deny policies fire on
the ``tool_call`` phase (``event.type == "tool_call"``), so the audit must
evaluate at that phase to observe them. The server *can* park a ``tool_call``
``ASK`` server-side (URL elicitation) for a non-read-only caller, which would
block a synchronous POST; the forwarder therefore evaluates with a **bounded
timeout** and treats a timeout as fail-open (logged, no warning) so the
transcript mirror never hangs on a parked ASK. An immediate DENY (and any ASK
returned directly, e.g. a read-only deployment) is surfaced as a warning. This
is best-effort and fail-open by design (R3/R4 in the design doc).
"""

from __future__ import annotations

from collections.abc import Mapping

from omnigent.native_policy_hook import hook_payload_to_evaluation_request

# Harness label stamped onto the audit ``EvaluationRequest`` context so server
# policies can recognize antigravity-native as the originating surface.
HARNESS_NAME = "antigravity-native"

# The one-time degrade-notice text. Mirrors codex-native's ``policy_notice_pending``
# degrade banner (``codex_native_app_server._disable_policy_hook``): it tells the
# user that, unlike the blocking native gates, this harness only audits tool
# calls *after* they execute.
DEGRADE_NOTICE_TEXT = (
    "Antigravity native enforcement is audit-only; tool calls are not blocked "
    "before execution. Policy violations are surfaced as warnings after the tool "
    "has already run (agy exposes no pre-execution policy hook)."
)

# agy transcript step fields (kept local so this module does not depend on the
# forwarder's private constants).
_FIELD_SOURCE = "source"
_FIELD_TYPE = "type"
_FIELD_TOOL_CALLS = "tool_calls"
_SOURCE_MODEL = "MODEL"
_TYPE_PLANNER_RESPONSE = "PLANNER_RESPONSE"

# ``tool_calls[].args`` carries these display-only keys alongside the real tool
# arguments; they are dropped from the evaluated ``tool_input`` so a policy sees
# the genuine arguments (mirrors the forwarder's ``_TOOL_ARG_DISPLAY_KEYS``).
_TOOL_ARG_DISPLAY_KEYS = frozenset({"toolAction", "toolSummary"})

# Verdicts that constrain a tool. ``DENY`` is a hard block; ``ASK`` would demand
# approval — but this harness cannot hold the tool (it already ran), so ASK is
# treated DENY-style (surfaced as a warning), never deferred. ``ALLOW`` /
# ``UNSPECIFIED`` are non-violations.
_VIOLATION_ACTIONS = frozenset({"POLICY_ACTION_DENY", "POLICY_ACTION_ASK"})

_AGENT_NAME = "antigravity-native-ui"


def step_to_audit_tool_calls(step: Mapping[str, object]) -> list[dict[str, object]]:
    """
    Extract neutral ``{tool_name, tool_input}`` records from one agy step.

    Only a ``MODEL`` / ``PLANNER_RESPONSE`` step initiates tools (every other
    step is user input, a tool *result*, or system noise — none of which is a
    fresh tool call). Each ``tool_calls`` entry is reduced to the neutral shape
    the policy translation layer expects, with agy's display-only ``args`` keys
    (``toolAction`` / ``toolSummary``) stripped so the policy sees the real
    arguments.

    :param step: One parsed agy transcript step object.
    :returns: Ordered ``{"tool_name": str, "tool_input": dict}`` records — one
        per valid tool call — or ``[]`` when the step initiates no tools.
    """
    if step.get(_FIELD_SOURCE) != _SOURCE_MODEL:
        return []
    if step.get(_FIELD_TYPE) != _TYPE_PLANNER_RESPONSE:
        return []
    tool_calls = step.get(_FIELD_TOOL_CALLS)
    if not isinstance(tool_calls, list):
        return []
    records: list[dict[str, object]] = []
    for entry in tool_calls:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_args = entry.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        tool_input = {
            key: value for key, value in args.items() if key not in _TOOL_ARG_DISPLAY_KEYS
        }
        records.append({"tool_name": name, "tool_input": tool_input})
    return records


def build_audit_evaluation_request(
    *,
    tool_name: str,
    tool_input: Mapping[str, object],
    model: str | None,
) -> dict[str, object] | None:
    """
    Build the proto ``EvaluationRequest`` for a post-hoc tool-call audit.

    Reuses the harness-neutral
    :func:`omnigent.native_policy_hook.hook_payload_to_evaluation_request` with a
    ``PreToolUse`` payload so the request lands on the ``PHASE_TOOL_CALL`` phase
    (the phase tool-name / cost / CEL deny policies fire on). ``mcp__omnigent__*``
    tools return ``None`` (already relay-enforced via ``ProxyMcpManager`` — see
    the translation layer), so the forwarder skips them; connector-native MCP
    tools (e.g. ``mcp__github__*``) are NOT relay-enforced and still produce a
    request. The request ``context`` is stamped with
    ``harness = "antigravity-native"`` and, when known, the ``model`` so a
    model-scoped policy evaluates against the user's current agy model.

    :param tool_name: agy tool name, e.g. ``"run_command"``.
    :param tool_input: The tool's arguments (display-only keys already stripped
        by :func:`step_to_audit_tool_calls`).
    :param model: The session's agy model label, e.g. ``"gemini-2.5-pro"``, or
        ``None`` when unknown (then ``context.model`` is omitted).
    :returns: An ``EvaluationRequest`` dict to POST to ``/policies/evaluate``, or
        ``None`` when the tool is not policy-relevant (an ``mcp__omnigent__*``
        tool).
    """
    request = hook_payload_to_evaluation_request(
        "PreToolUse",
        {"tool_name": tool_name, "tool_input": dict(tool_input)},
    )
    if request is None:
        return None
    event = request.get("event")
    if isinstance(event, dict):
        context = event.get("context")
        if not isinstance(context, dict):
            context = {}
            event["context"] = context
        context["harness"] = HARNESS_NAME
        if model is not None:
            context["model"] = model
    return request


def audit_verdict_is_violation(eval_response: Mapping[str, object]) -> bool:
    """
    Return whether an ``EvaluationResponse`` is a tool-call policy violation.

    A violation is ``POLICY_ACTION_DENY`` or ``POLICY_ACTION_ASK`` (ASK is
    treated DENY-style — the tool already ran, so it cannot be held for
    approval). ``POLICY_ACTION_ALLOW`` / ``POLICY_ACTION_UNSPECIFIED`` (and any
    unknown verdict) are not violations.

    :param eval_response: Parsed ``EvaluationResponse`` from ``/policies/evaluate``.
    :returns: ``True`` when the verdict constrains the tool.
    """
    return str(eval_response.get("result", "")) in _VIOLATION_ACTIONS


def audit_violation_warning_text(eval_response: Mapping[str, object]) -> str:
    """
    Render the human-readable warning text for a violation verdict.

    Frames the warning honestly as post-hoc: the tool has *already executed*.
    Includes the policy reason when present.

    :param eval_response: Parsed ``EvaluationResponse`` (already classified a
        violation by :func:`audit_verdict_is_violation`).
    :returns: Warning text, e.g.
        ``"[Policy violation] blocked by policy — this tool call already
        executed (antigravity-native enforcement is audit-only)."``.
    """
    reason = eval_response.get("reason")
    reason_text = (
        str(reason) if isinstance(reason, str) and reason else "tool call denied by policy"
    )
    return (
        f"[Policy violation] {reason_text} — this tool call already executed "
        "(antigravity-native enforcement is audit-only)."
    )


def build_policy_violation_item(
    *,
    conversation_id: str,
    step_index: int,
    call_ordinal: int,
    text: str,
) -> dict[str, object]:
    """
    Build the ``external_conversation_item`` data for a policy-violation warning.

    Surfaced as an assistant message so it renders inline in the chat where the
    offending tool call appears. The response id is namespaced by conversation +
    step + the per-call ordinal + a ``policy`` suffix so it never collides with
    the mirrored tool item AND stays distinct when a single ``PLANNER_RESPONSE``
    step carries multiple violating tool calls (keying on ``step_index`` alone
    would collide two same-step violations onto one id).

    :param conversation_id: agy conversation id, e.g. ``"68caaeac-..."``.
    :param step_index: The transcript ``step_index`` the violating tool call came
        from.
    :param call_ordinal: Zero-based position of this tool call within its step
        (``0`` for the first ``tool_calls`` entry, ``1`` for the second, ...), so
        two violations from the same step get distinct response ids.
    :param text: Warning text from :func:`audit_violation_warning_text`.
    :returns: The ``data`` payload for a ``{"type": "external_conversation_item",
        "data": ...}`` event.
    """
    return {
        "item_type": "message",
        "item_data": {
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": text}],
        },
        "response_id": f"agy_{conversation_id}_{step_index}_{call_ordinal}_policy",
    }


def build_degrade_notice_item(*, conversation_id: str) -> dict[str, object]:
    """
    Build the ``external_conversation_item`` data for the one-time degrade notice.

    Posted once per forwarder lifetime to tell the user this harness only audits
    tool calls *after* they run (it cannot block). Mirrors codex-native's
    ``policy_notice_pending`` degrade banner, adapted to a conversation item
    because antigravity-native has no terminal-ensure response to piggyback on.

    :param conversation_id: agy conversation id, e.g. ``"68caaeac-..."``.
    :returns: The ``data`` payload for a ``{"type": "external_conversation_item",
        "data": ...}`` event.
    """
    return {
        "item_type": "message",
        "item_data": {
            "role": "assistant",
            "agent": _AGENT_NAME,
            "content": [{"type": "output_text", "text": DEGRADE_NOTICE_TEXT}],
        },
        "response_id": f"agy_{conversation_id}_audit_notice",
    }
