"""Built-in LLM routing policies.

Gates expensive LLM calls by classifying the user's message as
trivial or non-trivial via ``event["llm_client"]``. Requires the
server ``--config`` ``llm:`` block; abstains when absent.

Classification results are cached in ``session_state`` by message
hash so repeated ``llm_request`` round-trips within a turn pay
for only one classifier call. See
``examples/server_config_deny_trivial_opus.yaml`` for usage.

:func:`intent_based_authorization` implements intent-based permissioning: it records
the user's first message as the authoritative intent for the session,
then gates every subsequent ``tool_call`` against that intent using the
server-level LLM client.  Tool calls that cannot plausibly serve the
original intent are denied before they run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

_log = logging.getLogger(__name__)

# Session-state key prefix for cached classification results.
# Full key is ``_routing_classification:<sha256-of-message>``.
_CACHE_KEY_PREFIX = "_routing_classification:"

_DEFAULT_CLASSIFICATION_PROMPT = (
    "You are a task-difficulty classifier. Given the user's message below, "
    "decide whether it is a TRIVIAL task (simple factual lookup, greeting, "
    "short Q&A, trivial code change, status check) or a COMPLEX task "
    "(multi-step reasoning, complex analysis, large code refactor, "
    "open-ended research, nuanced writing)."
)

# Responses API structured output schema for the classifier.
# Forces the model to return ``{"difficulty": "TRIVIAL"}`` or
# ``{"difficulty": "COMPLEX"}`` — no free-text parsing needed.
_CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "format": {
        "type": "json_schema",
        "name": "difficulty_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "difficulty": {
                    "type": "string",
                    "enum": ["TRIVIAL", "COMPLEX"],
                },
            },
            "required": ["difficulty"],
            "additionalProperties": False,
        },
    },
}


def _extract_response_text(response: Any) -> str:
    """
    Extract the text content from an LLM response.

    Handles two shapes:

    - ``output_text`` property (OpenAI SDK ``Response``).
    - ``output[0].content[0].text`` (omnigent
      :class:`~omnigent.llms.types.Response`).

    :param response: The response object from
        ``PolicyLLMClient.create()``.
    :returns: The extracted text, or empty string when the
        response shape is unrecognized or empty.
    """
    # Try the convenience property first (OpenAI SDK shape).
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    # Fall back to the structured shape.
    output = getattr(response, "output", None)
    if not isinstance(output, list) or not output:
        return ""
    first = output[0]
    content = getattr(first, "content", None)
    if not isinstance(content, list) or not content:
        return ""
    return getattr(content[0], "text", "") or ""


def deny_trivial_to_expensive_model(
    *,
    expensive_models: list[str],
    classification_prompt: str = _DEFAULT_CLASSIFICATION_PROMPT,
) -> PolicyCallable:
    """Factory: deny trivial tasks from using expensive models.

    Fires on ``llm_request`` events. When the request targets one
    of the *expensive_models*, classifies the ``last_user_message``
    as TRIVIAL or COMPLEX using the server-level LLM client with
    structured output. TRIVIAL tasks are denied so the harness
    surfaces the denial to the agent; COMPLEX tasks pass through.

    Non-expensive models, missing client, empty messages, and
    classification failures all pass through (fail open).

    :param expensive_models: Model ids that should not be used for
        trivial tasks, e.g. ``["databricks-claude-opus-4-6",
        "openai/o3"]``. Required — the operator must explicitly
        list the models to gate.
    :param classification_prompt: System instructions for the
        classifier LLM call. The model is constrained to respond
        with structured JSON
        (``{"difficulty": "TRIVIAL"|"COMPLEX"}``); the prompt
        only needs to describe the classification criteria, not
        the output format.
    :returns: An async policy callable that denies trivial
        ``llm_request`` events targeting expensive models.
    """
    gated = frozenset(expensive_models)

    async def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """Classify the user message and deny trivial calls to expensive models.

        Uses ``session_state`` to cache classification results keyed
        by a SHA-256 hash of the user message. Within a turn, the
        ``llm_request`` phase fires once per LLM round-trip (tool
        call → LLM → tool call → LLM …), but the user message is
        unchanged across round-trips — the cache avoids redundant
        classifier calls.

        :param event: Policy event dict.
        :returns: DENY when the task is classified as TRIVIAL and
            the model is expensive; ``None`` (abstain) otherwise.
        """
        if event.get("type") != "llm_request":
            return None

        data = event.get("data")
        if not isinstance(data, dict):
            return None

        current_model = data.get("model", "")
        if current_model not in gated:
            return None

        user_message = data.get("last_user_message", "")
        if not isinstance(user_message, str) or not user_message.strip():
            return None

        # ── Cache lookup ────────────────────────────────────────
        msg_hash = hashlib.sha256(user_message.encode()).hexdigest()[:16]
        cache_key = f"{_CACHE_KEY_PREFIX}{msg_hash}"
        state = event.get("session_state") or {}
        cached = state.get(cache_key)

        if cached == "TRIVIAL":
            return {
                "result": "DENY",
                "reason": (
                    f"This task appears trivial and does not warrant "
                    f"the expensive model '{current_model}'. Use a "
                    f"smaller model for simple tasks."
                ),
            }
        if cached == "COMPLEX":
            return None

        # ── Classification ──────────────────────────────────────
        llm_client = event.get("llm_client")
        if llm_client is None:
            _log.warning(
                "deny_trivial_to_expensive_model: event['llm_client'] is None — "
                "server has no llm: config. Abstaining."
            )
            return None

        try:
            response = await llm_client.create(
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_message}],
                    },
                ],
                instructions=classification_prompt,
                text=_CLASSIFICATION_SCHEMA,
            )
            raw_text = _extract_response_text(response)
            if not raw_text:
                return None
            classification = json.loads(raw_text)
        except Exception:  # noqa: BLE001 — catch-all for LLM/JSON failures; fail-open
            _log.exception("deny_trivial_to_expensive_model: classification call failed")
            return None

        difficulty = (
            classification.get("difficulty", "") if isinstance(classification, dict) else ""
        )

        # ── Cache + decide ──────────────────────────────────────
        if difficulty == "TRIVIAL":
            _log.info(
                "deny_trivial_to_expensive_model: classified as TRIVIAL — "
                "denying call to expensive model %s",
                current_model,
            )
            return {
                "result": "DENY",
                "reason": (
                    f"This task appears trivial and does not warrant "
                    f"the expensive model '{current_model}'. Use a "
                    f"smaller model for simple tasks."
                ),
                "state_updates": [
                    {"key": cache_key, "action": "set", "value": "TRIVIAL"},
                ],
            }

        if difficulty == "COMPLEX":
            return {
                "result": "ALLOW",
                "state_updates": [
                    {"key": cache_key, "action": "set", "value": "COMPLEX"},
                ],
            }

        return None

    return evaluate  # type: ignore[return-value]


# ── intent_based_authorization ───────────────────────────────────────────────────────────────

# Session-state key that stores the user's original intent (first message).
_INTENT_KEY = "_intent_based_authorization_intent"

# Session-state key prefix for per-tool-call verdict cache.
# Full key: ``_intent_based_authorization_check:<hex16-of-intent+tool+args>``.
_INTENT_CHECK_PREFIX = "_intent_based_authorization_check:"


def _off_task_reason(tool_name: str, intent: str) -> str:
    return (
        f"Tool call '{tool_name}' may not be consistent with the "
        f"session's original task. The agent was asked to: "
        f"{intent[:200]}"
    )


_DEFAULT_INTENT_CHECK_PROMPT = """\
You are a security policy enforcer for an AI agent.

The agent was given a specific task (the "original intent") at the start of the
session. Your job is to decide whether a proposed tool call is consistent with
completing that task, or whether it goes beyond / outside the task scope.

Be permissive for sub-tasks and helper steps that clearly serve the original
intent. Only flag tool calls that have no plausible connection to the stated
goal (e.g. the user asked to "fix a login bug" and the agent is about to send
an email to an external address).

Return strict JSON only:
{"verdict": "ON_TASK" | "OFF_TASK"}
"""

_INTENT_CHECK_SCHEMA: dict[str, Any] = {
    "format": {
        "type": "json_schema",
        "name": "intent_check_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["ON_TASK", "OFF_TASK"],
                },
            },
            "required": ["verdict"],
            "additionalProperties": False,
        },
    },
}


def intent_based_authorization() -> PolicyCallable:
    """Factory: enforce intent-based permissioning across the session.

    Implements a two-phase policy:

    1. **``request`` phase (intent capture)** — the very first user message
       is recorded in ``session_state`` as the authoritative intent for the
       session.  Subsequent ``request`` events are ignored (the intent is
       immutable once set).

    2. **``tool_call`` phase (intent check)** — every tool call is evaluated
       against the stored intent using the server-level LLM client.  If the
       tool call has no plausible connection to the original task, it is
       denied before the tool runs.

    Classification results are cached in ``session_state`` keyed by a hash
    of the intent + tool name + serialised arguments.  Identical tool calls
    within a session pay for only one classifier round-trip.

    No arguments are required.  All behaviour is configurable via the
    optional *classification_prompt* parameter.

    Requires the server to have an ``llm:`` config block; abstains
    (fail-open) when no LLM client is available.

    .. note::
        This is a best-effort control, not a cryptographic guarantee.
        A sophisticated adversary can craft inputs that convince the
        classifier the call is ``ON_TASK``.  Use in combination with other
        policies (e.g. Bell-LaPadula) for defence-in-depth.

    :param classification_prompt: System instructions for the intent-check
        LLM call.  Describes the ``ON_TASK`` / ``OFF_TASK`` classification
        criteria; the output schema is enforced via structured output.
    :returns: An async policy callable that fires on ``request`` (intent
        capture) and ``tool_call`` (intent check) events.

    YAML usage::

        policies:
          intent_based_authorization:
            type: function
            function:
              path: omnigent.policies.builtins.routing.intent_based_authorization
    """
    # intent_based_authorization takes no required arguments — it is a zero-config factory.
    # The inner evaluate() closes over nothing from the outer scope except
    # the classification prompt; we define it as a nested async function.

    async def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """Capture intent on first request; gate tool calls against it.

        :param event: Policy event dict.
        :returns: ASK when a tool call is classified as OFF_TASK; ``None``
            (abstain) on all other phases and on fail-open conditions.
        """
        phase = event.get("type")

        # ── Phase 1: capture intent from the first user message ───────────
        if phase == "request":
            state = event.get("session_state") or {}
            if state.get(_INTENT_KEY):
                return None  # intent already recorded — nothing to do

            message = event.get("data", "")
            if not isinstance(message, str) or not message.strip():
                return None

            return {
                "result": "ALLOW",
                "state_updates": [
                    {
                        "key": _INTENT_KEY,
                        "action": "set",
                        "value": message.strip()[:1000],
                    }
                ],
            }

        # ── Phase 2: check tool calls against the stored intent ───────────
        if phase != "tool_call":
            return None

        state = event.get("session_state") or {}
        intent: str = state.get(_INTENT_KEY, "")
        if not intent:
            return None  # no intent captured yet — fail open

        tool_name: str = event.get("target") or ""
        data = event.get("data") or {}
        tool_args = data.get("arguments", {}) if isinstance(data, dict) else {}

        # ── Cache lookup ─────────────────────────────────────────────────
        args_repr = json.dumps(tool_args, sort_keys=True, default=str)
        check_hash = hashlib.sha256(
            f"{intent}\x00{tool_name}\x00{args_repr}".encode()
        ).hexdigest()[:16]
        cache_key = f"{_INTENT_CHECK_PREFIX}{check_hash}"
        cached = state.get(cache_key)

        if cached == "OFF_TASK":
            return {
                "result": "ASK",
                "reason": _off_task_reason(tool_name, intent),
            }
        if cached == "ON_TASK":
            return None

        # ── Classification ────────────────────────────────────────────────
        llm_client = event.get("llm_client")
        if llm_client is None:
            _log.warning(
                "intent_based_authorization: no llm_client — "
                "server has no llm: config. Abstaining."
            )
            return None

        user_prompt = (
            f"Original intent: {intent[:500]}\n\n"
            f"Proposed tool call: {tool_name}\n"
            f"Arguments: {args_repr[:500]}"
        )

        try:
            response = await llm_client.create(
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}],
                    }
                ],
                instructions=_DEFAULT_INTENT_CHECK_PROMPT,
                text=_INTENT_CHECK_SCHEMA,
            )
            raw_text = _extract_response_text(response)
            if not raw_text:
                return None
            verdict_obj = json.loads(raw_text)
        except Exception:  # noqa: BLE001 — fail-open on LLM/JSON errors
            _log.exception("intent_based_authorization: classification call failed")
            return None

        verdict = verdict_obj.get("verdict", "") if isinstance(verdict_obj, dict) else ""

        if verdict == "OFF_TASK":
            _log.info(
                "intent_based_authorization: OFF_TASK — ASK tool_call %s (intent: %.80s…)",
                tool_name,
                intent,
            )
            return {
                "result": "ASK",
                "reason": _off_task_reason(tool_name, intent),
                "state_updates": [
                    {"key": cache_key, "action": "set", "value": "OFF_TASK"},
                ],
            }

        if verdict == "ON_TASK":
            return {
                "result": "ALLOW",
                "state_updates": [
                    {"key": cache_key, "action": "set", "value": "ON_TASK"},
                ],
            }

        return None  # unrecognised verdict — fail open

    return evaluate  # type: ignore[return-value]


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.routing.deny_trivial_to_expensive_model",
        "kind": "factory",
        "name": "Deny Trivial Tasks on Expensive Models",
        "description": (
            "Classifies the user's message as TRIVIAL or COMPLEX using "
            "the server-level LLM client with structured output. Denies "
            "TRIVIAL tasks from using expensive models (e.g. Opus, o3). "
            "Requires the server to have an llm: config block."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "expensive_models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Model ids to gate, e.g. ['databricks-claude-opus-4-6', 'openai/o3']."
                    ),
                },
                "classification_prompt": {
                    "type": "string",
                    "description": (
                        "System instructions for the classifier. Describes "
                        "classification criteria (output format is enforced "
                        "via structured output, not the prompt)."
                    ),
                },
            },
            "required": ["expensive_models"],
        },
    },
    {
        "handler": "omnigent.policies.builtins.routing.intent_based_authorization",
        "kind": "factory",
        "name": "Intent Based Authorization",
        "description": (
            "Enforces intent-based permissioning: records the user's first message "
            "as the authoritative session intent, then gates every tool call against "
            "that intent using the server-level LLM client. Tool calls that cannot "
            "plausibly serve the original task trigger an ASK prompt for human approval "
            "before they run. "
            "Classification results are cached in session_state to avoid redundant "
            "LLM calls for identical tool invocations. "
            "Requires an llm: config block on the server; abstains (fail-open) when "
            "no LLM client is available. Zero required parameters."
        ),
        "params_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]
