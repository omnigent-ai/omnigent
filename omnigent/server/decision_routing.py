"""Smart Routing with a System One decision model, deferring to the judge when unsure.

A decision model (TypeSafe Jev, or a local Jev-compatible server such as Laya, Von or
OpenJev) reads a text once and answers typed questions with probabilities instead of
generating text. Routing asks it one Choice question over the valid (harness, model)
candidates. A confident answer routes directly; anything else goes to the built-in LLM
judge, exactly as it would without this client.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from omnigent.server.smart_routing import RoutingResult, failure_detail, routing_last_error

if TYPE_CHECKING:
    from omnigent.server.smart_routing import RoutingClient

_logger = logging.getLogger(__name__)

#: A decision model answers in well under a second; a slow one should leave the
#: judge behind it most of the caller's routing budget.
DEFAULT_REQUEST_TIMEOUT_S = 3.0
#: Picks below this confidence defer to the judge. Tune it on your own traffic.
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
#: The same message budget the judge gets.
MAX_MESSAGE_CHARS = 4000

_QUESTION_ID = "route"
_INSTRUCTIONS = (
    "Each option is a harness and model that could handle the task below. Within a "
    "harness, models are listed from cheapest and weakest to most expensive and capable. "
    "Which is the cheapest option that would still handle this task fully and correctly?"
)


class DecisionModelRoutingClient:
    """Route with one ``/v1/systemone`` Choice question; defer to *fallback* when unsure.

    The call fails open: a transport error, an error status, a malformed answer, a pick
    outside the candidates, or a confidence below the threshold all hand the call to
    *fallback*. With no fallback the call returns ``None`` (not routed) and
    :attr:`last_error` says why.
    """

    #: Source stamped on decisions this client answers itself.
    router_source = "decision-model"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        fallback: RoutingClient | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        """
        :param base_url: Decision-model server root, e.g. ``"https://api.typesafe.ai"``
            or ``"http://localhost:8000"``; ``/v1/systemone`` is appended.
        :param model: Model id sent with each request, e.g. ``"jev-1.13.0"``.
        :param api_key: Bearer token, or ``None`` for an unauthenticated local server.
        :param confidence_threshold: Minimum answer confidence to route directly.
        :param fallback: The client to defer to, normally the built-in LLM judge.
        :param request_timeout: Seconds allowed for the decision-model call.
        """
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._model = model
        self._api_key = api_key
        self._threshold = confidence_threshold
        self._fallback = fallback
        self._timeout = request_timeout
        #: Reason the most recent route() returned None; see RoutingClient.
        self.last_error: str | None = None
        #: Which router answered the most recent call: :attr:`router_source`, or
        #: the fallback's source when the call was deferred.
        self.last_source: str | None = None

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        """Pick a (harness, model) for *message*, deferring to the fallback when unsure.

        :param message: The task text to route on.
        :param available_models: Harness id → candidate model ids, cheapest first.
        :returns: The verdict, or ``None`` when neither this client nor the
            fallback routed the call.
        """
        self.last_error = None
        self.last_source = self.router_source
        options = _options(available_models)
        if not options:
            self.last_error = "no candidate models were available"
            return None
        result, reason = await self._ask(message, options)
        if result is not None:
            return result
        return await self._defer(message, available_models, reason)

    async def _ask(
        self,
        message: str,
        options: dict[str, tuple[str, str, str]],
    ) -> tuple[RoutingResult | None, str]:
        """Ask the decision model; return its verdict, or ``None`` and why it has none."""
        import httpx

        body = {
            "model": self._model,
            "state": message[:MAX_MESSAGE_CHARS],
            "questions": {
                _QUESTION_ID: {
                    "type": "choice",
                    "instructions": _INSTRUCTIONS,
                    "criteria": {label: desc for label, (_, _, desc) in options.items()},
                }
            },
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                resp = await http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return None, f"decision model request failed: {failure_detail(exc)}"
        if resp.status_code >= 400:
            _logger.warning(
                "DecisionModelRoutingClient: %s returned %s: %s",
                self._url,
                resp.status_code,
                resp.text[:500],
            )
            return None, f"decision model returned HTTP {resp.status_code}"
        try:
            answer = resp.json()["answers"][_QUESTION_ID]
            pick = answer["choice"]
        except (ValueError, KeyError, TypeError):
            return None, "decision model returned a malformed answer"
        if pick not in options:
            return None, f"decision model picked an unknown option {pick!r}"
        confidence = _unit_float(answer.get("confidence"))
        if confidence is None or confidence < self._threshold:
            return None, f"decision model confidence {confidence} is below {self._threshold}"
        harness, model, _ = options[pick]
        _logger.info(
            "DecisionModelRoutingClient: picked %s/%s confidence=%.2f top=%s",
            harness,
            model,
            confidence,
            _top(answer.get("probabilities")),
        )
        rationale = (
            f"The decision model picked {model} as the cheapest option that handles "
            f"this task (confidence {confidence:.2f})."
        )
        return RoutingResult(model=model, rationale=rationale, harness=harness), ""

    async def _defer(
        self,
        message: str,
        available_models: dict[str, list[str]],
        reason: str,
    ) -> RoutingResult | None:
        """Hand the call to the fallback, or decline it when there is none."""
        if self._fallback is None:
            self.last_error = reason
            return None
        _logger.info("DecisionModelRoutingClient: deferring to the judge (%s)", reason)
        self.last_source = getattr(self._fallback, "router_source", "oss-llm")
        result = await self._fallback.route(message, available_models)
        if result is None:
            self.last_error = routing_last_error(self._fallback) or reason
        return result


def _options(available_models: dict[str, list[str]]) -> dict[str, tuple[str, str, str]]:
    """Label every valid (harness, model) pair for the Choice question.

    :returns: Option label → ``(harness, model, description)``, cheapest first
        within each harness, e.g. ``"codex / gpt-5.5"`` →
        ``("codex", "gpt-5.5", "model 2 of 3 in the codex harness ...")``.
    """
    options: dict[str, tuple[str, str, str]] = {}
    for harness, models in available_models.items():
        for i, model in enumerate(models):
            desc = f"model {i + 1} of {len(models)} in the {harness} harness (1 is the cheapest)"
            options[f"{harness} / {model}"] = (harness, model, desc)
    return options


def _unit_float(value: Any) -> float | None:  # type: ignore[explicit-any]  # JSON value
    """*value* as a finite float in [0, 1], else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def _top(
    probabilities: Any,  # type: ignore[explicit-any]  # JSON value
    n: int = 3,
) -> list[tuple[str, float]]:
    """The *n* most probable options, for the routing log."""
    if not isinstance(probabilities, dict):
        return []
    scored = [(k, p) for k, v in probabilities.items() if (p := _unit_float(v)) is not None]
    return sorted(scored, key=lambda kv: kv[1], reverse=True)[:n]
