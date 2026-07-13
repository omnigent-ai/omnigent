"""Built-in CEL expression policy.

A factory that compiles a user-submitted CEL expression into a
policy callable. CEL is non-Turing-complete, side-effect-free,
and guaranteed to terminate — no sandbox escapes, no infinite
loops, no file I/O.

The expression receives the full ``PolicyEvent`` dict as an
``event`` variable and must return a map with a ``result`` key
(``"DENY"``, ``"ASK"``, or ``"ALLOW"``) and an optional
``"reason"`` key. Non-map returns abstain.

Register via the session policy API::

    POST /v1/sessions/{session_id}/policies
    {
        "name": "block_shell",
        "type": "python",
        "handler": "omnigent.policies.builtins.cel.cel_policy",
        "factory_params": {
            "expression": "event.type == \\"tool_call\\" && event.data.name == \\"sys_os_shell\\"",
            "reason": "Shell access is blocked."
        }
    }

CEL reference: https://cel.dev/overview/cel-overview
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from cel_expr_python import cel as _cel
except ImportError:
    _cel = None  # type: ignore[assignment]

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_log = logging.getLogger(__name__)


def cel_policy(
    *,
    expression: str,
    reason: str = "Denied by policy.",
) -> PolicyCallable:
    """Factory: compile a CEL expression into a policy callable.

    The expression must return a map with a ``result`` key
    (``"DENY"``, ``"ASK"``, or ``"ALLOW"``) and an optional
    ``"reason"`` key. Returning ``None`` or a map without a
    valid ``result`` abstains (ALLOW).

    :param expression: CEL expression evaluated per policy event.
        The ``event`` variable is the full
        :class:`~omnigent.policies.schema.PolicyEvent` dict.
        Must return a map, e.g.::

            event.type == "tool_call"
              ? {"result": "ASK", "reason": "Approve?"}
              : {"result": "ALLOW"}

    :param reason: Fallback reason for DENY/ASK results when
        the map omits a ``"reason"`` key, e.g.
        ``"Shell access is blocked."``.
    :returns: A policy callable following the
        :class:`PolicyCallable` contract.
    :raises ValueError: If the expression has CEL syntax errors.
    """
    if _cel is None:
        raise ImportError(
            "cel-expr-python is required for CEL policies but is not installed. "
            "Install it with: pip install cel-expr-python"
        )

    env = _cel.NewEnv(variables={"event": _cel.Type.DYN})
    try:
        compiled = env.compile(expression)
    except RuntimeError as exc:
        # cel-expr-python raises bare RuntimeError for all compile
        # failures (syntax errors, undeclared references, etc.) — it
        # does not expose a more specific exception type.
        _log.warning("CEL compile error: %s", exc)
        raise ValueError(f"CEL policy: compile error in expression: {exc}") from exc

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Evaluate the CEL expression against a policy event.

        The expression must return a map with a ``result`` key
        (``"ALLOW"``, ``"DENY"``, or ``"ASK"``). An optional
        ``"reason"`` key overrides the factory default. Any
        other return shape (including bool) abstains.

        :param event: The policy event dict.
        :returns: A :class:`PolicyResponse` dict, or ``None``
            to abstain.
        """
        result = compiled.eval(data={"event": dict(event)})

        # Eval errors (missing field, type mismatch) → abstain.
        if result.type() == _cel.Type.ERROR:
            _log.debug(
                "CEL policy eval error on event type %r, abstaining",
                event.get("type"),
            )
            return None

        raw = result.value()
        if not isinstance(raw, dict):
            return None

        response: dict[str, str] = {k: v.plain_value() for k, v in raw.items()}
        verdict = response.get("result", "").upper()
        if verdict not in ("DENY", "ASK", "ALLOW"):
            return None

        out: PolicyResponse = {"result": verdict}  # type: ignore[typeddict-item]
        if "reason" in response:
            out["reason"] = response["reason"]
        elif verdict != "ALLOW":
            out["reason"] = reason
        return out

    return evaluate  # type: ignore[return-value]


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = (
    []
    if _cel is None
    else [
        {
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "kind": "factory",
            "name": "CEL Expression Policy",
            "description": (
                "Evaluate a CEL (Common Expression Language) expression against "
                "every policy event. The expression receives the full event as "
                '`event` and must return a map with `result` ("DENY", "ASK", or '
                '"ALLOW") and optional `reason` keys. '
                "CEL is non-Turing-complete and side-effect-free."
            ),
            "params_schema": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "CEL expression. The `event` variable holds the PolicyEvent dict. "
                            "Must return a map: "
                            '{"result": "DENY"|"ASK"|"ALLOW", "reason": "..."}. '
                            "Event fields: "
                            'event.type ("request"|"tool_call"|"tool_result"|'
                            '"response"|"llm_request"|"llm_response"|"output_logged"); '
                            "event.target (tool name on tool_call/tool_result, null otherwise); "
                            "event.data (phase-specific: string for request/response, "
                            '{"name": str, "arguments": map} for tool_call, '
                            '{"result": any} for tool_result, '
                            '{"model": str, "messages_count": int, "tools_count": int,'
                            ' "system_prompt_preview": str, "last_user_message": str}'
                            " for llm_request); "
                            "event.context.actor.run_as (user email); "
                            "event.context.usage.total_cost_usd (session spend). "
                            "Example: "
                            'event.type == "tool_call" && event.data.name == "sys_os_shell" '
                            '? {"result": "DENY", "reason": "Shell blocked."} '
                            ': {"result": "ALLOW"}'
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Fallback reason for DENY/ASK when the map omits a reason key."
                        ),
                        "default": "Denied by policy.",
                    },
                },
                "required": ["expression"],
            },
        },
    ]
)
