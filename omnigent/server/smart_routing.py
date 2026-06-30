"""Server-side intelligent model routing.

Infers available models from the session's harness type and delegates
the routing decision to the :class:`RoutingClient` on
:attr:`RuntimeCaps.routing_client`.  The default implementation
(:class:`LLMRoutingClient`) calls the server-level LLM with a prompt
that describes each model's capabilities directly — no tier abstraction.
Managed deployments can swap in a different implementation via
``RuntimeCaps``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

_logger = logging.getLogger(__name__)

# ── Model lists per harness family ──────────────────────────────────────────
#
# Ordered cheapest → most powerful within each family.

MODEL_LISTS: dict[str, list[str]] = {
    "claude": [
        "databricks-claude-haiku-4-5",
        "databricks-claude-sonnet-4-6",
        "databricks-claude-opus-4-8",
    ],
    "gpt": [
        "databricks-gpt-5-4-nano",
        "databricks-gpt-5-4-mini",
        "databricks-gpt-5-4",
        "databricks-gpt-5-5",
    ],
    # pi is multi-model: Claude and GPT both available.
    "pi": [
        "databricks-gpt-5-4-nano",
        "databricks-claude-haiku-4-5",
        "databricks-gpt-5-4-mini",
        "databricks-claude-sonnet-4-6",
        "databricks-gpt-5-4",
        "databricks-claude-opus-4-8",
        "databricks-gpt-5-5",
    ],
}

_HARNESS_FAMILY: dict[str, str] = {
    "claude-sdk": "claude",
    "claude_sdk": "claude",
    "claude-native": "claude",
    "pi": "pi",
    "codex": "gpt",
    "codex-native": "gpt",
    "openai-agents": "gpt",
    "openai-agents-sdk": "gpt",
    "agents_sdk": "gpt",
}


def infer_models(harness: str | None) -> list[str] | None:
    """Return available models for *harness*, or ``None`` if unroutable."""
    if harness is None:
        return None
    family = _HARNESS_FAMILY.get(harness)
    if family is None:
        return None
    return MODEL_LISTS.get(family)


# ── RoutingClient protocol ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingResult:
    """The routing client's recommendation.

    :param model: Model id to use, e.g. ``"databricks-claude-opus-4-8"``.
    :param rationale: One-sentence explanation from the judge.
    """

    model: str
    rationale: str


class RoutingClient(Protocol):
    """Protocol for pluggable model routing implementations."""

    async def route(
        self,
        message: str,
        available_models: list[str],
    ) -> RoutingResult | None:
        """Pick the best model for a session's initial message.

        :param message: The user's first message text.
        :param available_models: Model ids available for this harness,
            ordered cheapest → most powerful.
        :returns: A :class:`RoutingResult`, or ``None`` to skip routing.
        """
        ...


# ── Default LLM-based implementation ───────────────────────────────────────

_JUDGE_SYSTEM_TEMPLATE = """\
You are a model router for a coding assistant. Given the user's message,
pick the model best suited for the task.

Available models (ordered fastest/cheapest to most capable/expensive):
{model_menu}

Model naming conventions:
- Claude family: haiku is the fastest and most lightweight; sonnet is
  a balanced mid-range; opus is the most powerful and thorough.
- GPT family: nano is the most lightweight, then mini, then the base
  model; higher version numbers (e.g. gpt-5-5 vs gpt-5-4) indicate
  newer, more capable releases.
- The list order reflects relative speed, cost, and capability for
  this deployment — models later in the list are slower and more
  expensive but produce higher-quality results on complex tasks.

Trade-off guidance:
- Simple tasks (greetings, quick lookups, one-line fixes) are handled
  well by faster models with minimal quality loss.
- Moderately complex tasks (single-file edits, debugging, explanation)
  benefit from a mid-range model.
- Deeply complex tasks (multi-file refactors, architecture decisions,
  security analysis, long reasoning chains) are where the most capable
  models make a meaningful difference.

Return **strict JSON only**:
{{"model": "<id>", "rationale": "<one sentence>"}}
"""


def _build_rubric(available_models: list[str]) -> str:
    """Format the judge prompt with the ordered model list."""
    lines = [f"- {m}" for m in available_models]
    return _JUDGE_SYSTEM_TEMPLATE.format(model_menu="\n".join(lines))


_VERDICT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "model": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["model", "rationale"],
    "additionalProperties": False,
}


class LLMRoutingClient:
    """Default routing client using the server-level PolicyLLMClient."""

    def __init__(self, llm_client: Any) -> None:  # type: ignore[explicit-any]
        self._llm = llm_client

    async def route(
        self,
        message: str,
        available_models: list[str],
    ) -> RoutingResult | None:
        rubric = _build_rubric(available_models)
        try:
            response = await self._llm.create(
                instructions=rubric,
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": message[:4000]}],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "routing_verdict",
                        "strict": True,
                        "schema": _VERDICT_SCHEMA,
                    }
                },
            )
            text = response.output[0].content[0].text
            _logger.info("LLMRoutingClient: raw response: %s", text[:500])
            verdict = json.loads(text)
        except Exception:  # noqa: BLE001  # fail-open
            _logger.warning("LLMRoutingClient: judge call failed", exc_info=True)
            return None

        model = verdict.get("model")
        rationale = verdict.get("rationale", "")
        if not model or not isinstance(model, str):
            return None

        # Clamp hallucinated models to the cheapest available.
        if model not in available_models:
            if available_models:
                _logger.info(
                    "LLMRoutingClient: clamping unknown model %r to %s",
                    model,
                    available_models[0],
                )
                model = available_models[0]
            else:
                return None

        return RoutingResult(model=model, rationale=str(rationale))


# ── Public API ──────────────────────────────────────────────────────────────


async def route_turn(
    harness: str | None,
    user_message: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Pick the best model for a turn via :attr:`RuntimeCaps.routing_client`."""
    models = infer_models(harness)
    if models is None:
        return None, None

    try:
        from omnigent.runtime._globals import _caps
    except ImportError:
        return None, None

    if _caps is None or _caps.routing_client is None:
        return None, None

    result = await _caps.routing_client.route(user_message, models)
    if result is None:
        return None, None

    _logger.info(
        "smart_routing: model=%s rationale=%s",
        result.model,
        result.rationale,
    )
    return result.model, {"model": result.model, "rationale": result.rationale}
