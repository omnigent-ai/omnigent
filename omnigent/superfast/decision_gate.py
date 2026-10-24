"""Superfast Decision Gate: a System One front-door classifier.

A small, non-autoregressive decision model (Von, or any Jev-compatible
server) reads the pending user turn in a single forward pass and returns
typed, calibrated answers (noul / choice) without generating text. The
gate turns those answers into a conservative routing recommendation so a
harness can skip expensive System Two work when the decision is obvious.

This is the first, shadow-only increment. The contract:

* Off by default. Nothing runs unless ``SUPERFAST_ENABLED`` is truthy.
* Shadow mode. The gate classifies and logs its recommendation only. It
  never changes routing, never skips the model call, and never alters any
  user-visible behaviour.
* Fail open. Any error, timeout, non-2xx response, unreachable backend,
  or malformed body yields "no opinion" and the caller continues exactly
  as if the gate were off. It never raises into the agent loop.
* No heavy new dependency. The backend is a plain HTTP POST to a
  Jev-compatible endpoint using ``httpx``, which the project already
  depends on. The decision model is installed out of band, not bundled.

Concept and reference implementation by Andrea Bruno, CC BY 4.0
(https://github.com/Graphene-Lab/harness-superfast). The decision models
themselves (Von, OpenJev, Laya) are third-party open models; only the
integration architecture and routing method are ours.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

_logger = logging.getLogger(__name__)

#: Master switch. The gate is inert unless this env var is truthy.
ENABLED_ENV = "SUPERFAST_ENABLED"
#: Full URL of the decision endpoint.
ENDPOINT_ENV = "SUPERFAST_ENDPOINT"
#: Model id sent in the request body.
MODEL_ENV = "SUPERFAST_MODEL"
#: Hard timeout for a single decision call, in milliseconds.
TIMEOUT_MS_ENV = "SUPERFAST_TIMEOUT_MS"

_DEFAULT_ENDPOINT = "http://localhost:8000/v1/systemone"
_DEFAULT_MODEL = "von-1.2.0"
_DEFAULT_TIMEOUT_MS = 150

#: Values accepted as "on" for the boolean env toggle.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Hostnames that refer to this machine.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


@dataclass(frozen=True)
class GateSettings:
    """Resolved runtime configuration for the gate.

    :param enabled: Master switch; when False the gate is never invoked.
    :param endpoint: Full URL, e.g. ``http://localhost:8000/v1/systemone``.
    :param model: Model id sent in the request body, e.g. ``von-1.2.0``.
    :param timeout_ms: Hard timeout for one decision call, in milliseconds.
    """

    enabled: bool
    endpoint: str
    model: str
    timeout_ms: int


def resolve_settings() -> GateSettings:
    """Read the gate configuration from the environment.

    Every field falls back to a safe default. A malformed timeout is
    ignored rather than fatal, so a bad override can never break the agent.

    :returns: The resolved settings for this process.
    """
    return GateSettings(
        enabled=os.environ.get(ENABLED_ENV, "").strip().lower() in _TRUTHY,
        endpoint=os.environ.get(ENDPOINT_ENV, "").strip() or _DEFAULT_ENDPOINT,
        model=os.environ.get(MODEL_ENV, "").strip() or _DEFAULT_MODEL,
        timeout_ms=_read_timeout_ms(),
    )


def _read_timeout_ms() -> int:
    """Parse the timeout override, falling back to the default on any error."""
    raw = os.environ.get(TIMEOUT_MS_ENV, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_MS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_MS
    return value if 0 < value <= 60_000 else _DEFAULT_TIMEOUT_MS


def _is_loopback(endpoint: str) -> bool:
    """Return whether *endpoint* points at this machine."""
    try:
        host = httpx.URL(endpoint).host.lower()
    except Exception:  # noqa: BLE001 - a bad URL is simply not loopback
        return False
    return host in _LOOPBACK_HOSTS or host.endswith(".localhost")


#: Standard question set for classifying one incoming user turn. Kept small
#: so the single forward pass stays well under the timeout budget.
_TURN_QUESTIONS: dict[str, dict[str, Any]] = {
    "needs_tool": {
        "type": "noul",
        "instructions": (
            "Does answering this request require taking an action with a tool "
            "(reading, writing, running, searching), rather than replying from "
            "what is already known?"
        ),
    },
    "answerable_from_context": {
        "type": "noul",
        "instructions": (
            "Can this request be answered from information already present in "
            "the conversation, without any new investigation?"
        ),
    },
    "intent": {
        "type": "choice",
        "instructions": "Classify the primary intent of the user request.",
        "criteria": {
            "code_change": "Create, edit, or delete code or files.",
            "code_question": "Explain or reason about code without changing it.",
            "command": "Run a command or operation.",
            "chat": "Casual conversation or a question needing no tools.",
            "other": "None of the above.",
        },
    },
}

#: Minimum calibrated intent confidence for the plain_chat fast route.
_PLAIN_CHAT_CONFIDENCE_FLOOR = 0.5

#: A routing recommendation derived from a turn's decision answers.
TurnRoute = Literal["needs_tool", "answer_from_context", "plain_chat", "unknown"]


@dataclass(frozen=True)
class TurnDecision:
    """A turn-level decision: the derived route plus its latency.

    :param route: The conservative routing recommendation.
    :param latency_ms: Wall-clock milliseconds spent on the decision call.
    """

    route: TurnRoute
    latency_ms: int


async def query_system_one(
    state: str,
    questions: dict[str, dict[str, Any]],
    settings: GateSettings,
) -> dict[str, Any] | None:
    """Issue one System One request and return its parsed answers.

    Fail-open: returns ``None`` on any transport error, timeout, non-2xx
    response, or malformed body. Never raises.

    :param state: The user message text to classify.
    :param questions: The typed question set to ask.
    :param settings: Resolved gate configuration.
    :returns: The decoded ``answers`` mapping, or ``None`` on any failure.
    """
    try:
        # The decision backend is a local, out-of-band server. The process
        # proxy env is meant for LLM traffic and must not tunnel this local
        # call, so trust_env is disabled for loopback endpoints.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_ms / 1000.0),
            trust_env=not _is_loopback(settings.endpoint),
        ) as client:
            resp = await client.post(
                settings.endpoint,
                json={"model": settings.model, "state": state, "questions": questions},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        body = resp.json()
    except Exception:  # noqa: BLE001 - fail open on any error or timeout
        return None
    if not isinstance(body, dict):
        return None
    answers = body.get("answers")
    return answers if isinstance(answers, dict) else None


def _read_noul(answer: Any) -> float | None:
    """Return a noul probability only when it is a real finite value in [0, 1].

    Anything else (absent, non-numeric, NaN, infinite, out of range) reads as
    "no evidence", so a mis-scaled or missing answer can never produce a
    decisive fast route.
    """
    if not isinstance(answer, dict):
        return None
    value = answer.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _confident(answer: Any, floor: float) -> bool:
    """Return whether *answer* carries a calibrated confidence at or above *floor*."""
    if not isinstance(answer, dict):
        return False
    conf = answer.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return False
    if conf != conf or conf in (float("inf"), float("-inf")):
        return False
    return 0.0 <= conf <= 1.0 and conf >= floor


def derive_route(answers: dict[str, Any]) -> TurnRoute:
    """Derive a conservative route from the decision answers.

    A fast route is recommended only when the relevant numbers are decisive;
    otherwise the route is ``unknown`` so the caller falls back to the
    normal path.

    :param answers: The decoded ``answers`` mapping from the decision model.
    :returns: The routing recommendation.
    """
    needs_tool = _read_noul(answers.get("needs_tool"))
    from_context = _read_noul(answers.get("answerable_from_context"))

    # A decisive "needs a tool" wins first: the harness must not skip work.
    if needs_tool is not None and needs_tool >= 0.85:
        return "needs_tool"

    # Strongly answerable from context, with a present and low tool-need signal.
    if (
        from_context is not None
        and from_context >= 0.85
        and needs_tool is not None
        and needs_tool <= 0.3
    ):
        return "answer_from_context"

    # Clearly chat, with a calibrated intent and a present, low tool-need signal.
    intent = answers.get("intent")
    if (
        isinstance(intent, dict)
        and intent.get("choice") == "chat"
        and _confident(intent, _PLAIN_CHAT_CONFIDENCE_FLOOR)
        and needs_tool is not None
        and needs_tool <= 0.2
    ):
        return "plain_chat"

    return "unknown"


async def classify_turn(text: str, settings: GateSettings) -> TurnDecision | None:
    """Classify a user turn through the gate.

    :param text: The user message text.
    :param settings: Resolved gate configuration.
    :returns: A :class:`TurnDecision`, or ``None`` when the backend gave no
        usable answer (fail open).
    """
    started = time.monotonic()
    answers = await query_system_one(text, _TURN_QUESTIONS, settings)
    latency_ms = int((time.monotonic() - started) * 1000)
    if answers is None:
        return None
    return TurnDecision(route=derive_route(answers), latency_ms=latency_ms)


def _text_from_content(content: Any) -> str:
    """Join user text from a message body's ``content`` blocks.

    :param content: A message ``content`` list, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :returns: The concatenated text, or ``""`` when there is none.
    """
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts).strip()


#: Strong references to in-flight shadow tasks so the event loop does not
#: garbage-collect them mid-run (RUF006). Each task removes itself on done.
_pending: set[asyncio.Task[None]] = set()


async def _shadow_classify_and_log(
    text: str, settings: GateSettings, session_id: str | None
) -> None:
    """Run the gate in shadow mode and log the recommendation.

    Never raises: a shadow classification must not surface into the agent.

    :param text: The user message text.
    :param settings: Resolved gate configuration.
    :param session_id: Session id for log attribution, if known.
    """
    extra = {"session_id": session_id} if session_id else None
    try:
        decision = await classify_turn(text, settings)
    except Exception:  # noqa: BLE001 - shadow mode must never surface an error
        _logger.debug("superfast gate: shadow classification errored", exc_info=True, extra=extra)
        return
    if decision is None:
        _logger.info("superfast gate (shadow): no opinion (fail-open)", extra=extra)
        return
    _logger.info(
        "superfast gate (shadow): route=%s latency_ms=%d",
        decision.route,
        decision.latency_ms,
        extra=extra,
    )


def maybe_shadow_gate(content: Any, session_id: str | None = None) -> None:
    """Fire-and-forget a shadow classification of a pending user turn.

    Returns immediately. When the gate is disabled or the turn has no text,
    it does nothing. Otherwise it schedules a background task that
    classifies the turn and logs the recommended route, adding no latency
    to the real turn and changing no behaviour.

    :param content: The message body's ``content`` blocks.
    :param session_id: Session id for log attribution, if known.
    """
    settings = resolve_settings()
    if not settings.enabled:
        return
    text = _text_from_content(content)
    if not text:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_shadow_classify_and_log(text, settings, session_id))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
