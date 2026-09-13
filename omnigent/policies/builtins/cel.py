"""Built-in CEL expression policy.

A factory that compiles a user-submitted CEL expression into a
policy callable. CEL is non-Turing-complete, side-effect-free,
and guaranteed to terminate — no sandbox escapes, no infinite
loops, no file I/O.

The expression receives the full ``PolicyEvent`` dict as an
``event`` variable and must return a map with a ``result`` key
(``"DENY"``, ``"ASK"``, or ``"ALLOW"``) and an optional
``"reason"`` and/or ``"state_updates"`` key. Non-map returns abstain.
``state_updates`` must be a list of maps shaped
``{"key": string, "action": "set"|"increment"|"delete"|"append", "value": any}``.
``value`` is required for ``set``, ``increment``, and ``append``, and ignored
for ``delete``.

Register statically in an agent's YAML, or dynamically on a running
session via the policy API.

Static, in an agent ``config.yaml`` (``policies:`` block, parsed by
:mod:`omnigent.inner.loader` — ``handler`` + ``factory_params``)::

    policies:
      block_shell:
        type: function
        handler: omnigent.policies.builtins.cel.cel_policy
        factory_params:
          expression: 'event.type == "tool_call" && event.data.name == "sys_os_shell"'
          reason: Shell access is blocked.

Static, in a bundled agent spec (``guardrails.policies``, parsed by
:mod:`omnigent.spec.parser` — note the different spelling: a
``function`` mapping with ``path`` + ``arguments``; this parser does
NOT read ``factory_params``)::

    guardrails:
      policies:
        block_shell:
          type: function
          function:
            path: omnigent.policies.builtins.cel.cel_policy
            arguments:
              expression: 'event.type == "tool_call" && event.data.name == "sys_os_shell"'
              reason: Shell access is blocked.

Dynamic, via the session policy API::

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
from collections.abc import Mapping, Sequence

try:
    import celpy
    import celpy.celtypes
    from celpy.adapter import json_to_cel
    from celpy.celparser import CELParseError
    from celpy.evaluation import CELEvalError
except ImportError:
    celpy = None  # type: ignore[assignment]

from omnigent.policies.schema import (
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
    request_user_text,
)

_log = logging.getLogger(__name__)


def cel_policy(
    *,
    expression: str,
    reason: str = "Denied by policy.",
) -> PolicyCallable:
    """Factory: compile a CEL expression into a policy callable.

    The expression must return a map with a ``result`` key
    (``"DENY"``, ``"ASK"``, or ``"ALLOW"``) and an optional
    ``"reason"`` and/or ``"state_updates"`` key. Returning ``None``
    or a map without a valid ``result`` abstains (ALLOW).
    ``state_updates`` must be a list of maps shaped
    ``{"key": string, "action": "set"|"increment"|"delete"|"append", "value": any}``.
    ``value`` is required for ``set``, ``increment``, and ``append``,
    and ignored for ``delete``.

    :param expression: CEL expression evaluated per policy event.
        The ``event`` variable is the full
        :class:`~omnigent.policies.schema.PolicyEvent` dict.
        Must return a map, e.g.::

            event.type == "tool_call"
              ? {"result": "ASK", "reason": "Approve?"}
              : {"result": "ALLOW"}

            {"result": "ALLOW",
             "state_updates": [
                 {"key": "call_count", "action": "increment", "value": 1}
             ]}

    :param reason: Fallback reason for DENY/ASK results when
        the map omits a ``"reason"`` key, e.g.
        ``"Shell access is blocked."``.
    :returns: A policy callable following the
        :class:`PolicyCallable` contract.
    :raises ValueError: If the expression has CEL syntax errors.
    """
    if celpy is None:
        raise ImportError(
            "cel-python is required for CEL policies but is not installed. "
            "Install it with: pip install cel-python"
        )

    env = celpy.Environment()
    try:
        ast = env.compile(expression)
    except CELParseError as exc:
        _log.warning("CEL compile error: %s", exc)
        raise ValueError(f"CEL policy: compile error in expression: {exc}") from exc

    prog = env.program(ast)
    _result_key = celpy.celtypes.StringType("result")
    _reason_key = celpy.celtypes.StringType("reason")
    _state_updates_key = celpy.celtypes.StringType("state_updates")

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        # llm_client is a live object used by Python policy callables;
        # CEL expressions cannot call methods on it and json_to_cel would
        # raise ValueError trying to convert it.
        cel_event = {k: v for k, v in event.items() if k != "llm_client"}
        # REQUEST-phase ``data`` is now the structured dict
        # ({"user_content", "attachments"}) from the input gate, but CEL
        # expressions authored against the request phase expect the user text as
        # a bare string (e.g. ``event.data.contains("secret")``). Project it back
        # to that string so existing request-phase CEL policies keep matching — a
        # map here would silently fail-open (string ops raise -> abstain -> ALLOW,
        # and ``==`` against a string is just false).
        if event.get("type") == "request":
            cel_event["data"] = request_user_text(event.get("data"))
        try:
            result = prog.evaluate({"event": json_to_cel(cel_event)})
        except (CELEvalError, ValueError, TypeError):
            _log.debug(
                "CEL policy eval error on event type %r, abstaining",
                event.get("type"),
            )
            return None

        if not isinstance(result, celpy.celtypes.MapType):
            return None

        if _result_key not in result:
            return None
        verdict = str(result[_result_key]).upper()
        if verdict not in ("DENY", "ASK", "ALLOW"):
            return None

        out: PolicyResponse = {"result": verdict}  # type: ignore[typeddict-item]
        if _reason_key in result:
            out["reason"] = str(result[_reason_key])
        elif verdict != "ALLOW":
            out["reason"] = reason
        if _state_updates_key in result:
            state_updates = _plain_cel_value(result[_state_updates_key])
            if not isinstance(state_updates, list):
                raise TypeError("CEL policy: state_updates must be a list of maps.")
            out["state_updates"] = state_updates  # type: ignore[typeddict-item]
        return out

    return evaluate


def _plain_cel_value(value: object) -> object:
    """
    Convert CEL wrapper values back to JSON-like Python values.

    :param value: A value returned from ``celpy`` evaluation.
    :returns: A plain ``dict``/``list``/scalar value suitable for
        :class:`FunctionPolicy` response coercion.
    """
    if celpy is not None:
        if isinstance(value, celpy.celtypes.BoolType):
            return bool(value)
        if isinstance(value, celpy.celtypes.NullType):
            return None
        if isinstance(value, celpy.celtypes.StringType):
            return str(value)
        if isinstance(value, celpy.celtypes.BytesType):
            return bytes(value)
        if isinstance(value, (celpy.celtypes.IntType, celpy.celtypes.UintType)):
            return int(value)
        if isinstance(value, celpy.celtypes.DoubleType):
            return float(value)
    if isinstance(value, Mapping):
        return {str(_plain_cel_value(k)): _plain_cel_value(v) for k, v in value.items()}
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, Sequence):
        return [_plain_cel_value(item) for item in value]
    return value


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, object]] = (
    []
    if celpy is None
    else [
        {
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "kind": "factory",
            "name": "CEL Expression Policy",
            "description": (
                "Evaluate a CEL (Common Expression Language) expression against "
                "every policy event. The expression receives the full event as "
                '`event` and must return a map with `result` ("DENY", "ASK", or '
                '"ALLOW") plus optional `reason` and/or `state_updates` keys. '
                "`state_updates` must be a list of maps with `key`, `action`, "
                'and `value`; valid actions are "set", "increment", "delete", '
                'and "append". `value` is ignored for "delete". '
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
                            '{"result": "DENY"|"ASK"|"ALLOW", "reason": "...", '
                            '"state_updates": [...]}. '
                            "`state_updates` must be a list of maps shaped "
                            '{"key": string, "action": '
                            '"set"|"increment"|"delete"|"append", "value": any}; '
                            "`value` is required for set/increment/append and ignored "
                            "for delete. "
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
