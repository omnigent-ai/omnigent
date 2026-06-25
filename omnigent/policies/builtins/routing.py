"""Built-in LLM routing policies.

Gates expensive LLM calls by classifying the user's message as
trivial or non-trivial via ``event["llm_client"]``. Requires the
server ``--config`` ``llm:`` block; abstains when absent.

Classification results are cached in ``session_state`` by message
hash so repeated ``llm_request`` round-trips within a turn pay
for only one classifier call. See
``examples/server_config_deny_trivial_opus.yaml`` for usage.
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


# ── Cost-control tier classifier ────────────────────────────────────────────

# Session-state key: set to ``"1"`` after the first classification so
# subsequent REQUEST evaluations short-circuit without an LLM call.
_CC_JUDGED_KEY = "_cost_control_judged"


def cost_control_classifier(
    *,
    rubric: str,
    tiers: dict[str, str | dict[str, str]],
) -> PolicyCallable:
    """Factory: classify the session's first user message into a cost tier.

    Fires on ``request`` events. On the first user message of a
    session (gated by ``session_state["_cost_control_judged"]``),
    classifies the prompt into one of the configured tiers using the
    server-level ``PolicyLLMClient``. The verdict is persisted as
    ``cost_control.tier``, ``cost_control.model``, and (when
    configured) ``cost_control.harness`` labels via ``set_labels``,
    then re-read by the sessions route to apply ``model_override`` /
    ``harness_override``.

    On subsequent turns the classifier short-circuits (returns ALLOW
    with no LLM call). On classification failure the classifier
    degrades gracefully (ALLOW, no labels — the session runs on the
    agent's declared model).

    :param rubric: Author-supplied guidance for the classification,
        e.g. ``"Use tier_3 for multi-step coding; tier_1 for simple
        lookups."``. The tier names used in the rubric must match
        the keys in *tiers*.
    :param tiers: Mapping of tier name → model config. Each value is
        either a bare model-id string (model-only routing) or a dict
        with ``model`` and optional ``harness`` (cross-harness
        routing). At least two tiers must be defined. Examples::

            # Two tiers, model-only:
            {"cheap": "databricks-claude-haiku-4-5",
             "expensive": "databricks-claude-opus-4-8"}

            # Three tiers with harness routing:
            {"tier_1": "databricks-claude-haiku-4-5",
             "tier_2": {"model": "databricks-claude-sonnet-4-6"},
             "tier_3": {"model": "databricks-claude-opus-4-8",
                        "harness": "claude-sdk"}}

    :returns: An async policy callable that always ALLOWs but may
        write ``cost_control.tier``, ``cost_control.model``, and
        ``cost_control.harness`` labels on the first turn.
    """
    # Normalize tiers into model + optional harness lookups.
    tier_names = sorted(tiers.keys())
    tier_to_model: dict[str, str] = {}
    tier_to_harness: dict[str, str | None] = {}
    for name, value in tiers.items():
        if isinstance(value, str):
            tier_to_model[name] = value
            tier_to_harness[name] = None
        else:
            tier_to_model[name] = value["model"]
            tier_to_harness[name] = value.get("harness")

    # Build the prompt fragment listing valid tier names.
    tier_choices = " or ".join(f'{{"tier": "{t}"}}' for t in tier_names)

    async def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """Classify on first request, short-circuit on later turns.

        :param event: Policy event dict.
        :returns: ALLOW with ``set_labels`` on the first user message;
            ``None`` (abstain) on non-request events or after the first
            classification.
        """
        if event.get("type") != "request":
            return None

        # Once-per-session gate: skip if already classified.
        state = event.get("session_state") or {}
        if state.get(_CC_JUDGED_KEY):
            return None

        user_text = event.get("data")
        if not isinstance(user_text, str) or not user_text.strip():
            return None

        llm_client = event.get("llm_client")
        if llm_client is None:
            _log.warning(
                "cost_control_classifier: event['llm_client'] is None — "
                "server has no llm: config. Skipping classification."
            )
            return None

        try:
            response = await llm_client.create(
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_text}],
                    },
                ],
                instructions=(
                    "You are a cost-control router for an AI agent. Decide "
                    "which tier should handle the user request below.\n\n"
                    f"Routing rubric:\n{rubric}\n\n"
                    f"Respond with ONLY a JSON object: {tier_choices}. "
                    "No other text."
                ),
            )
            raw_text = _extract_response_text(response)
            # Strip markdown code fences the model may wrap around JSON.
            raw_text = raw_text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            if not raw_text:
                _log.warning("cost_control_classifier: empty LLM response")
                return _ALLOW
            classification = json.loads(raw_text)
        except Exception:  # noqa: BLE001 — degrade on ANY failure
            _log.exception("cost_control_classifier: classification call failed")
            return _ALLOW

        tier = classification.get("tier", "") if isinstance(classification, dict) else ""
        if tier not in tier_to_model:
            _log.warning(
                "cost_control_classifier: unexpected tier %r (valid: %s); skipping",
                tier,
                tier_names,
            )
            return _ALLOW

        model = tier_to_model[tier]
        harness = tier_to_harness[tier]
        _log.info(
            "cost_control_classifier: classified as %r (model=%s, harness=%s)",
            tier,
            model,
            harness or "<executor default>",
        )
        labels: dict[str, str] = {
            "cost_control.tier": tier,
            "cost_control.model": model,
        }
        if harness is not None:
            labels["cost_control.harness"] = harness
        return {
            "result": "ALLOW",
            "set_labels": labels,
            "state_updates": [
                {"key": _CC_JUDGED_KEY, "action": "set", "value": "1"},
            ],
        }

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
        "handler": "omnigents.policies.builtins.routing.cost_control_classifier",
        "kind": "factory",
        "name": "Cost Control Tier Classifier",
        "description": (
            "Classifies the session's first user message into one of the "
            "configured tiers. Writes the verdict as cost_control.tier, "
            "cost_control.model, and (optionally) cost_control.harness "
            "labels. Always ALLOWs — this is a classifier, not a gate. "
            "Requires the server to have an llm: config block."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "rubric": {
                    "type": "string",
                    "description": (
                        "Author-supplied classification guidance. Tier names "
                        "in the rubric must match the keys in 'tiers'."
                    ),
                },
                "tiers": {
                    "type": "object",
                    "description": (
                        "Mapping of tier name to model config. Each value is "
                        "a model id string or {model, harness?} dict. "
                        "Example: {cheap: 'model-a', medium: 'model-b', "
                        "expensive: {model: 'model-c', harness: 'claude-sdk'}}"
                    ),
                    "additionalProperties": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "model": {"type": "string"},
                                    "harness": {"type": "string"},
                                },
                                "required": ["model"],
                            },
                        ],
                    },
                },
            },
            "required": ["rubric", "tiers"],
        },
    },
]
