"""Server-side intelligent model routing.

Infers available model tiers from the session's harness type and
delegates the routing decision to the :class:`RoutingClient` on
:attr:`RuntimeCaps.routing_client`.  The default implementation
(:class:`LLMRoutingClient`) calls the server-level LLM (same
``llm:`` config block that policies use).  Managed deployments
can swap in a different implementation via ``RuntimeCaps``.

The verdict is applied as ``model_override`` on the runner body
before the first turn is forwarded — the runner sees a concrete
model, not a routing config.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

_logger = logging.getLogger(__name__)

# ── Tier templates ──────────────────────────────────────────────────────────

TIER_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "claude": {
        "cheap": ["databricks-claude-haiku-4-5"],
        "medium": ["databricks-claude-sonnet-4-6"],
        "expensive": ["databricks-claude-opus-4-8"],
    },
    "gpt": {
        "cheap": ["databricks-gpt-5-4-mini"],
        "medium": ["databricks-gpt-5-4"],
        "expensive": ["databricks-gpt-5-5"],
    },
}

_HARNESS_FAMILY: dict[str, str] = {
    "claude-sdk": "claude",
    "claude_sdk": "claude",
    "claude-native": "claude",
    "codex": "gpt",
    "codex-native": "gpt",
    "openai-agents": "gpt",
    "openai-agents-sdk": "gpt",
    "agents_sdk": "gpt",
}

_VALID_TIERS = frozenset({"cheap", "medium", "expensive"})


def infer_tiers(harness: str | None) -> dict[str, list[str]] | None:
    """Return model tiers for *harness*, or ``None`` if unroutable."""
    if harness is None:
        return None
    family = _HARNESS_FAMILY.get(harness)
    if family is None:
        return None
    return TIER_TEMPLATES.get(family)


# ── RoutingClient protocol ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingResult:
    """The routing client's recommendation.

    :param model: Model id to use, e.g. ``"databricks-claude-opus-4-8"``.
    :param tier: Difficulty tier, e.g. ``"expensive"``.
    :param rationale: One-sentence explanation from the judge.
    """

    model: str
    tier: str
    rationale: str


class RoutingClient(Protocol):
    """Protocol for pluggable model routing implementations.

    Receives the user's initial message and the available model tiers
    (inferred from the harness), and returns a :class:`RoutingResult`
    recommending which model to use — or ``None`` to skip routing.

    The default implementation (:class:`LLMRoutingClient`) calls the
    server-level LLM as a lightweight judge.  Managed deployments can
    provide a different implementation (e.g. a rules engine, a remote
    service, or a fine-tuned classifier).
    """

    async def route(
        self,
        message: str,
        available_tiers: dict[str, list[str]],
    ) -> RoutingResult | None:
        """Pick the best model for a session's initial message.

        :param message: The user's first message text.
        :param available_tiers: Tier name → model ids, e.g.
            ``{"cheap": ["databricks-claude-haiku-4-5"], ...}``.
        :returns: A :class:`RoutingResult`, or ``None`` to skip
            routing (fail-open — the turn runs on the spec default).
        """
        ...


# ── Default LLM-based implementation ───────────────────────────────────────

_JUDGE_SYSTEM_TEMPLATE = """\
You are an intelligent model router for a coding assistant.  Given the
user's message, classify its difficulty and pick the best model.

Available tiers (cheapest first):
{tier_menu}

Classification guide:
- **cheap**: trivial questions, greetings, one-line lookups, clarifications,
  conversational follow-ups ("yes", "thanks", "go ahead").
- **medium**: focused single-file changes, writing tasks, moderate analysis,
  explaining code, standard debugging.
- **expensive**: multi-file refactors, architecture design, security audits,
  deep reasoning chains, performance optimization across modules.

Return **strict JSON only** — no markdown, no explanation outside the object:
{{"tier": "<name>", "model": "<id>", "rationale": "<one sentence>"}}
"""


_VERDICT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "tier": {
            "type": "string",
            "enum": ["cheap", "medium", "expensive"],
        },
        "model": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["tier", "model", "rationale"],
    "additionalProperties": False,
}


def _build_rubric(tiers: dict[str, list[str]]) -> str:
    """Format the judge system prompt with the tier menu."""
    tier_order = ["cheap", "medium", "expensive"]
    lines = []
    for name in tier_order:
        models = tiers.get(name, [])
        if models:
            lines.append(f"  {name}: {', '.join(models)}")
    return _JUDGE_SYSTEM_TEMPLATE.format(tier_menu="\n".join(lines))


class LLMRoutingClient:
    """Default routing client that calls the server-level LLM.

    Uses the :class:`~omnigent.policies.types.PolicyLLMClient` built
    from the server's ``llm:`` config — same credentials, same model.

    :param llm_client: The pre-built PolicyLLMClient instance.
    """

    def __init__(self, llm_client: Any) -> None:  # type: ignore[explicit-any]
        self._llm = llm_client

    async def route(
        self,
        message: str,
        available_tiers: dict[str, list[str]],
    ) -> RoutingResult | None:
        rubric = _build_rubric(available_tiers)
        try:
            response = await self._llm.create(
                instructions=rubric,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": message[:4000],
                            }
                        ],
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
        except Exception:  # noqa: BLE001  # fail-open: any LLM/parse error skips routing
            _logger.warning("LLMRoutingClient: judge call failed", exc_info=True)
            return None

        model = verdict.get("model")
        tier = verdict.get("tier")
        rationale = verdict.get("rationale", "")
        if not model or not isinstance(model, str):
            return None
        if tier not in _VALID_TIERS:
            return None

        # Clamp hallucinated models to the first in the tier.
        tier_models = available_tiers.get(str(tier), [])
        if model not in tier_models and tier_models:
            _logger.info(
                "LLMRoutingClient: clamping %r to %s for tier %s",
                model,
                tier_models[0],
                tier,
            )
            model = tier_models[0]

        return RoutingResult(
            model=model,
            tier=str(tier),
            rationale=str(rationale),
        )


# ── Public API ──────────────────────────────────────────────────────────────


async def route_turn(
    harness: str | None,
    user_message: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Pick the best model for a turn via :attr:`RuntimeCaps.routing_client`.

    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :param user_message: The user's message text.
    :returns: ``(model_id, verdict_dict)`` or ``(None, None)``.
    """
    tiers = infer_tiers(harness)
    if tiers is None:
        return None, None

    try:
        from omnigent.runtime._globals import _caps
    except ImportError:
        return None, None

    if _caps is None or _caps.routing_client is None:
        return None, None

    result = await _caps.routing_client.route(user_message, tiers)
    if result is None:
        return None, None

    _logger.info(
        "smart_routing: verdict tier=%s model=%s rationale=%s",
        result.tier,
        result.model,
        result.rationale,
    )
    return result.model, {
        "tier": result.tier,
        "model": result.model,
        "rationale": result.rationale,
    }
