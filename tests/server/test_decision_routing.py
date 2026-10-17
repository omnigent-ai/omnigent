"""Tests for the decision-model routing client.

It routes directly only on a confident pick among the valid candidates and hands
every other outcome to the judge behind it, so the contract is when it defers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from omnigent.server.decision_routing import MAX_MESSAGE_CHARS, DecisionModelRoutingClient
from omnigent.server.routing_backend import RoutingBackends, route_with_fallback, select_router
from omnigent.server.smart_routing import RoutingResult

_MODELS = {"codex": ["gpt-mini", "gpt-max"], "claude-native": ["haiku", "opus"]}
_JUDGE_VERDICT = RoutingResult(model="opus", rationale="judge", harness="claude-native")


class _Judge:
    """Stand-in for the built-in judge; counts calls."""

    def __init__(self, result: RoutingResult | None = _JUDGE_VERDICT) -> None:
        self.result = result
        self.calls = 0
        self.last_error: str | None = None

    async def route(
        self, message: str, available_models: dict[str, list[str]]
    ) -> RoutingResult | None:
        del message, available_models
        self.calls += 1
        if self.result is None:
            self.last_error = "judge declined"
        return self.result


def _patch_httpx(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    """Route every httpx.AsyncClient through *handler*."""
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args: Any, **kwargs: Any) -> Any:
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return patch("httpx.AsyncClient", factory)


_OPTIONS = ["codex / gpt-mini", "codex / gpt-max", "claude-native / haiku", "claude-native / opus"]
_DROP = object()


def _answer(choice: object, confidence: object = 0.9, **fields: object) -> httpx.Response:
    """A 200 answering the route question; *fields* add or (with ``_DROP``) remove keys."""
    route: dict[str, object] = {"type": "choice", "choice": choice, "confidence": confidence}
    route.update(fields)
    route = {k: v for k, v in route.items() if v is not _DROP}
    return httpx.Response(200, json={"model": "jev-1", "answers": {"route": route}})


def _distribution(choice: str, p: float) -> dict[str, float]:
    """*p* on *choice*, the rest shared evenly by the other candidates."""
    rest = (1.0 - p) / (len(_OPTIONS) - 1)
    return {label: (p if label == choice else rest) for label in _OPTIONS}


def _client(judge: Any = None, **kwargs: Any) -> DecisionModelRoutingClient:
    kwargs.setdefault("api_key", "secret")
    return DecisionModelRoutingClient(
        base_url="https://dm.example.invalid/", model="jev-1", fallback=judge, **kwargs
    )


@pytest.mark.asyncio
async def test_a_confident_pick_routes_without_the_judge() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _answer("codex / gpt-mini")

    judge = _Judge()
    client = _client(judge)
    with _patch_httpx(handler):
        result = await client.route("x" * (MAX_MESSAGE_CHARS + 500), _MODELS)

    assert result is not None
    assert (result.harness, result.model) == ("codex", "gpt-mini")
    assert judge.calls == 0
    assert client.last_source == "decision-model"
    request = seen[0]
    assert request.url == "https://dm.example.invalid/v1/systemone"
    assert request.headers["Authorization"] == "Bearer secret"
    body = json.loads(request.content)
    assert body["model"] == "jev-1"
    assert len(body["state"]) == MAX_MESSAGE_CHARS
    question = body["questions"]["route"]
    assert question["type"] == "choice"
    # Every valid (harness, model) pair is an option, cheapest first per harness.
    assert list(question["criteria"]) == [
        "codex / gpt-mini",
        "codex / gpt-max",
        "claude-native / haiku",
        "claude-native / opus",
    ]


def _raise_connect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (lambda r: _answer("codex / gpt-mini", confidence=0.2), "below"),
        (lambda r: _answer("codex / gpt-mini", confidence="high"), "below"),
        (lambda r: _answer("codex / gpt-mini", confidence=None), "below"),
        (lambda r: _answer("gemini / flash"), "unknown option"),
        (lambda r: _answer(["codex / gpt-mini"]), "unknown option"),
        (lambda r: _answer("codex / gpt-mini", type="score"), "not a choice"),
        # `answer_confidence`, when sent, decides over a confident `confidence`.
        (lambda r: _answer("codex / gpt-mini", answer_confidence=0.3), "below"),
        (lambda r: _answer("codex / gpt-mini", answer_confidence="high"), "below"),
        # Jev's shape: `confidence` decides, even when the pick's probability is higher.
        (
            lambda r: _answer(
                "codex / gpt-mini",
                confidence=0.45,
                probabilities=_distribution("codex / gpt-mini", 0.8),
            ),
            "below",
        ),
        (
            lambda r: _answer(
                "codex / gpt-mini",
                confidence=_DROP,
                probabilities=_distribution("codex / gpt-mini", 0.4),
            ),
            "below",
        ),
        (lambda r: _answer("codex / gpt-mini", probabilities=[0.9, 0.1]), "not an object"),
        (
            lambda r: _answer("codex / gpt-mini", probabilities={"codex / gpt-mini": 1.0}),
            "cover exactly",
        ),
        (
            lambda r: _answer(
                "codex / gpt-mini",
                probabilities={**_distribution("codex / gpt-mini", 0.9), "gemini / flash": 0.0},
            ),
            "cover exactly",
        ),
        (
            lambda r: _answer(
                "codex / gpt-mini",
                probabilities={**_distribution("codex / gpt-mini", 0.9), "codex / gpt-max": "x"},
            ),
            "not a number",
        ),
        (
            lambda r: _answer("codex / gpt-mini", probabilities=dict.fromkeys(_OPTIONS, 0.5)),
            "sum to",
        ),
        (
            lambda r: _answer(
                "codex / gpt-mini", probabilities=_distribution("codex / gpt-max", 0.9)
            ),
            "not the most probable",
        ),
        (lambda r: httpx.Response(200, json={"answers": {}}), "malformed"),
        (lambda r: httpx.Response(200, text="not json"), "malformed"),
        (lambda r: httpx.Response(503, text="overloaded"), "HTTP 503"),
        (_raise_connect, "request failed"),
    ],
    ids=[
        "low-confidence",
        "non-numeric-confidence",
        "missing-confidence",
        "unknown-option",
        "non-string-choice",
        "not-a-choice",
        "low-answer-confidence",
        "non-numeric-answer-confidence",
        "confidence-decides-over-pick-probability",
        "low-pick-probability-without-confidence",
        "probabilities-not-an-object",
        "probabilities-missing-candidates",
        "probabilities-extra-option",
        "probabilities-non-numeric",
        "probabilities-do-not-sum-to-one",
        "choice-disagrees-with-probabilities",
        "missing-answer",
        "non-json",
        "error-status",
        "transport-error",
    ],
)
@pytest.mark.asyncio
async def test_every_non_answer_defers_to_the_judge(handler: Any, reason: str) -> None:
    judge = _Judge()
    client = _client(judge)
    with _patch_httpx(handler):
        result = await client.route("task", _MODELS)
    assert result == _JUDGE_VERDICT
    assert judge.calls == 1
    assert client.last_source == "oss-llm"

    # Without a judge the same outcome declines, and says why.
    alone = _client()
    with _patch_httpx(handler):
        assert await alone.route("task", _MODELS) is None
    assert reason in (alone.last_error or "")


@pytest.mark.asyncio
async def test_a_laya_style_answer_is_gated_on_the_pick_probability_not_the_entropy() -> None:
    # Laya puts normalized entropy in `confidence` (low over four options even when one
    # clearly wins) and the calibrated probability of the answer in `answer_confidence`.
    laya = _answer(
        "claude-native / haiku",
        confidence=0.21,
        answer_confidence=0.7,
        probabilities=_distribution("claude-native / haiku", 0.7),
    )
    judge = _Judge()
    with _patch_httpx(lambda r: laya):
        result = await _client(judge).route("task", _MODELS)
    assert result is not None
    assert (result.harness, result.model, judge.calls) == ("claude-native", "haiku", 0)


@pytest.mark.parametrize(
    "fields",
    [
        {"type": _DROP},
        # Jev as sampled live: two-decimal probabilities, `confidence` below the pick's.
        {
            "confidence": 0.74,
            "probabilities": {
                "codex / gpt-mini": 0.8,
                "claude-native / haiku": 0.2,
                "codex / gpt-max": 0.0,
                "claude-native / opus": 0.0,
            },
        },
        {"probabilities": {**_distribution("codex / gpt-mini", 0.7), "codex / gpt-max": 0.0999}},
        # With neither confidence field, the pick's own probability decides.
        {"confidence": _DROP, "probabilities": _distribution("codex / gpt-mini", 0.7)},
    ],
    ids=["no-type-field", "jev-live-shape", "rounded-distribution", "pick-probability-only"],
)
@pytest.mark.asyncio
async def test_a_sound_answer_still_routes(fields: dict[str, object]) -> None:
    judge = _Judge()
    with _patch_httpx(lambda r: _answer("codex / gpt-mini", **fields)):
        result = await _client(judge).route("task", _MODELS)
    assert result is not None
    assert (result.model, judge.calls) == ("gpt-mini", 0)


@pytest.mark.asyncio
async def test_the_threshold_decides_between_routing_and_deferring() -> None:
    with _patch_httpx(lambda r: _answer("claude-native / haiku", confidence=0.6)):
        strict_judge = _Judge()
        strict = await _client(strict_judge, confidence_threshold=0.7).route("t", _MODELS)
        lenient_judge = _Judge()
        lenient = await _client(lenient_judge, confidence_threshold=0.5).route("t", _MODELS)
    assert (strict, strict_judge.calls) == (_JUDGE_VERDICT, 1)
    assert lenient is not None
    assert (lenient.model, lenient_judge.calls) == ("haiku", 0)


@pytest.mark.asyncio
async def test_a_judge_that_also_declines_passes_its_reason_through() -> None:
    client = _client(_Judge(result=None))
    with _patch_httpx(lambda r: _answer("codex / gpt-mini", confidence=0.1)):
        assert await client.route("task", _MODELS) is None
    assert client.last_error == "judge declined"


@pytest.mark.asyncio
async def test_no_candidates_declines_without_calling_anything() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    judge = _Judge()
    client = _client(judge)
    with _patch_httpx(handler):
        assert await client.route("task", {}) is None
    assert judge.calls == 0
    assert client.last_error == "no candidate models were available"


@pytest.mark.asyncio
async def test_a_local_server_needs_no_api_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _answer("codex / gpt-max")

    with _patch_httpx(handler):
        await _client(api_key=None).route("task", _MODELS)
    assert "Authorization" not in seen[0].headers


# ── The decision records who actually answered ─────────────────────────────


def test_the_selector_labels_a_decision_model_local_client() -> None:
    choice = select_router(RoutingBackends(local=_client(_Judge())), gateway_backed=False)
    assert choice is not None
    assert choice.source == "decision-model"


@pytest.mark.parametrize(
    ("confidence", "source"),
    [(0.9, "decision-model"), (0.1, "oss-llm")],
    ids=["answered", "deferred"],
)
@pytest.mark.asyncio
async def test_route_with_fallback_stamps_the_router_that_answered(
    confidence: float, source: str
) -> None:
    client = _client(_Judge())
    with _patch_httpx(lambda r: _answer("codex / gpt-mini", confidence=confidence)):
        call = await route_with_fallback(
            RoutingBackends(local=client), "task", _MODELS, gateway_backed=False
        )
    assert call is not None
    assert call.source == source
