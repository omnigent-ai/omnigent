"""Per-model gating and self-healing fallback for ``reasoning_effort``.

Some OpenAI-compatible providers accept the Chat Completions
``reasoning_effort`` parameter on only a subset of their models and
reject it elsewhere with HTTP 400 (xAI: "Argument not supported on
this model: reasoning_effort"). Forwarding it unconditionally fails
every reasoning-enabled turn on those models.

The strategy is optimistic-send with a self-healing fallback rather
than a hand-maintained allowlist:

- A small seed set of models with *observed* rejections is skipped up
  front. It is a round-trip optimization, not a correctness
  dependency: an unlisted model that rejects the parameter self-heals
  via the strip-and-retry fallback, at the cost of one wasted call.
- Every other model gets the parameter. When the provider rejects the
  call with a 400 naming the parameter, the client strips it, retries
  once, and records the rejection so later calls in this process skip
  the wasted round trip.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

# (provider, model) pairs with observed HTTP 400 rejections of
# ``reasoning_effort``. Exact ids only — a prefix would swallow models
# that do accept it (xAI's grok-4.x line does; bare grok-4 does not).
_SEED_REJECTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("xai", "grok-4"),
        ("xai", "grok-code-fast-1"),
        ("xai", "grok-4-fast-reasoning"),
    }
)

# Rejections learned from live provider 400s, so a process pays at most
# one wasted round trip per (provider, model).
_learned_rejections: set[tuple[str, str]] = set()


def accepts_reasoning_effort(provider: str, model: str) -> bool:
    """Return whether ``reasoning_effort`` should be sent to this model.

    :param provider: Provider identifier, e.g. ``"xai"``.
    :param model: Model id without provider prefix, e.g. ``"grok-4"``.
    :returns: ``False`` when the pair is a seeded or learned rejection.
    """
    key = (provider, model.lower())
    return key not in _SEED_REJECTIONS and key not in _learned_rejections


# Capability-rejection phrasings. A bare "support" is not enough: a
# *value*-validation 400 ("reasoning_effort must be one of the supported
# values: ...") also mentions support, and stripping the param there
# would mask the caller's error and durably disable a supported
# capability (the stripped retry succeeds, so the learn-after-retry
# guard cannot catch it).
_CAPABILITY_REJECTION_PHRASES = ("not supported", "does not support", "unsupported")

# Phrasings that mark a *value* rejection even when a capability phrase
# also appears (e.g. "Unsupported value 'xhigh' for reasoning_effort").
_VALUE_REJECTION_PHRASES = ("value", "must be one of")


def is_reasoning_effort_rejection(exc: Exception) -> bool:
    """Detect a provider 400 that rejects the ``reasoning_effort`` param.

    Observed bodies name the parameter and say the *parameter* is
    unsupported: ``"Argument not supported on this model:
    reasoning_effort"`` and ``"Model ... does not support parameter
    reasoningEffort"``. Both the snake_case and camelCase spellings are
    matched, and the body must carry a capability-rejection phrase
    ("not supported" / "does not support" / "unsupported") so neither
    an unrelated 400 that merely echoes the request nor a *value*
    rejection ("must be one of the supported values") triggers the
    fallback.

    :param exc: The exception raised by the provider call.
    :returns: ``True`` when the fallback should strip and retry.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code != 400:
        return False
    try:
        body = exc.response.text.lower()
    except Exception:  # an unreadable body is not a param rejection
        return False
    if "reasoning_effort" not in body.replace("reasoningeffort", "reasoning_effort"):
        return False
    if any(phrase in body for phrase in _VALUE_REJECTION_PHRASES):
        return False
    return any(phrase in body for phrase in _CAPABILITY_REJECTION_PHRASES)


def record_reasoning_effort_rejection(provider: str, model: str) -> None:
    """Cache a live rejection so later calls skip the parameter.

    :param provider: Provider identifier, e.g. ``"xai"``.
    :param model: Model id without provider prefix.
    """
    key = (provider, model.lower())
    if key in _learned_rejections:
        return
    _learned_rejections.add(key)
    _logger.warning(
        "%s/%s rejected reasoning_effort (HTTP 400); retrying without it and "
        "omitting it for this model from now on. If this repeats across runs, "
        "seed the model in reasoning_effort_support to skip the wasted call.",
        provider,
        model,
    )


def strip_rejected_reasoning_effort(
    extra: dict[str, Any],
    exc: Exception,
) -> dict[str, Any] | None:
    """Return a param-stripped copy of *extra* when the fallback applies.

    Nothing is learned here: the caller records the rejection only
    after the stripped retry is confirmed, so a 400 that merely looked
    like a param rejection self-corrects instead of durably disabling
    ``reasoning_effort`` for a model that supports it.

    :param extra: The Chat Completions extra-params dict that was sent.
    :param exc: The exception the provider call raised.
    :returns: A copy of *extra* without ``reasoning_effort`` when *exc*
        is the provider rejecting that parameter; ``None`` otherwise.
    """
    if "reasoning_effort" not in extra or not is_reasoning_effort_rejection(exc):
        return None
    return {k: v for k, v in extra.items() if k != "reasoning_effort"}


def clear_learned_rejections() -> None:
    """Reset the learned-rejection cache. Useful for tests."""
    _learned_rejections.clear()
