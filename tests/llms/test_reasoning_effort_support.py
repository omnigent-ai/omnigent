"""Tests for per-model ``reasoning_effort`` gating and the 400 fallback.

Covers ``omnigent/llms/reasoning_effort_support.py``: the seeded
rejections, the rejection-detection heuristics on live provider error
bodies, and the learn-and-skip cache.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.llms.reasoning_effort_support import (
    accepts_reasoning_effort,
    clear_learned_rejections,
    is_reasoning_effort_rejection,
    record_reasoning_effort_rejection,
    strip_rejected_reasoning_effort,
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """Isolate the learned-rejection cache across tests."""
    clear_learned_rejections()


def _http_error(status_code: int, body: str) -> httpx.HTTPStatusError:
    """Build an ``HTTPStatusError`` with the given status and body.

    :param status_code: HTTP status code, e.g. ``400``.
    :param body: Raw response body text.
    :returns: The constructed error.
    """
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=httpx.Request("POST", "http://test/v1/chat/completions"),
        response=httpx.Response(status_code, content=body.encode()),
    )


# ── accepts_reasoning_effort ─────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    ["grok-4", "grok-code-fast-1", "grok-4-fast-reasoning"],
)
def test_seeded_xai_models_are_rejected(model: str) -> None:
    """Models with observed xAI rejections are skipped up front."""
    assert not accepts_reasoning_effort("xai", model)


def test_seed_match_is_case_insensitive() -> None:
    """Model-id casing must not defeat the seed set."""
    assert not accepts_reasoning_effort("xai", "Grok-4")


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("xai", "grok-3-mini"),  # supported Grok model
        ("xai", "grok-4-someday-new"),  # unknown ids are optimistic
        ("groq", "llama-3.3-70b-versatile"),  # other providers pass through
        ("databricks", "grok-4"),  # seed is per-provider, not per-name
    ],
)
def test_unlisted_pairs_are_optimistic(provider: str, model: str) -> None:
    """Anything outside the seed/learned sets keeps the parameter."""
    assert accepts_reasoning_effort(provider, model)


def test_recorded_rejection_is_learned() -> None:
    """A recorded live rejection flips the gate for that pair only."""
    record_reasoning_effort_rejection("xai", "grok-new")
    assert not accepts_reasoning_effort("xai", "grok-new")
    assert accepts_reasoning_effort("xai", "grok-other")
    assert accepts_reasoning_effort("groq", "grok-new")


# ── is_reasoning_effort_rejection ────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        # xAI's observed snake_case form.
        '{"error": "Argument not supported on this model: reasoning_effort"}',
        # camelCase form seen from grok fast models.
        '{"error": "Model grok-4-1-fast does not support parameter reasoningEffort."}',
    ],
)
def test_param_rejection_bodies_detected(body: str) -> None:
    """Both observed provider spellings of the rejection are matched."""
    assert is_reasoning_effort_rejection(_http_error(400, body))


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        # Unrelated 400 — mentions neither support nor the param.
        (400, '{"error": "messages: field required"}'),
        # 400 that echoes the param without a support complaint.
        (400, '{"error": "invalid value for reasoning_effort: bogus"}'),
        # Value rejection — the *effort value* is wrong, not the
        # capability; stripping would mask the caller's error and the
        # successful stripped retry would durably disable the param.
        (400, '{"error": "reasoning_effort must be one of the supported values: low, high"}'),
        (400, '{"error": "Unsupported value \'xhigh\' for reasoning_effort."}'),
        # Body that echoes the param and mentions support of something
        # else without a capability-rejection phrase.
        (400, '{"error": "reasoning_effort requires a supporting beta header"}'),
        # Right body, wrong status — not a capability rejection.
        (503, '{"error": "Argument not supported on this model: reasoning_effort"}'),
    ],
)
def test_non_rejection_errors_not_matched(status_code: int, body: str) -> None:
    """Only a 400 naming the param as unsupported triggers the fallback."""
    assert not is_reasoning_effort_rejection(_http_error(status_code, body))


def test_non_http_errors_not_matched() -> None:
    """Transport errors and other exceptions never trigger the fallback."""
    assert not is_reasoning_effort_rejection(ValueError("boom"))
    assert not is_reasoning_effort_rejection(
        httpx.ConnectError("no route", request=httpx.Request("POST", "http://test"))
    )


# ── strip_rejected_reasoning_effort ──────────────────────────────────


def test_strip_returns_copy_without_param() -> None:
    """A matching rejection strips the param without mutating the input."""
    extra: dict[str, Any] = {"reasoning_effort": "low", "temperature": 0.5}
    exc = _http_error(400, "Argument not supported on this model: reasoning_effort")

    stripped = strip_rejected_reasoning_effort(extra, exc)

    assert stripped == {"temperature": 0.5}
    assert extra["reasoning_effort"] == "low", "the original dict must not be mutated"


def test_strip_does_not_learn() -> None:
    """Stripping alone learns nothing — the caller records only after a
    confirmed retry, so a false-positive match self-corrects."""
    extra: dict[str, Any] = {"reasoning_effort": "low"}
    exc = _http_error(400, "Argument not supported on this model: reasoning_effort")

    assert strip_rejected_reasoning_effort(extra, exc) is not None
    assert accepts_reasoning_effort("xai", "grok-new")


def test_strip_declines_when_param_absent() -> None:
    """A 400 on a request that never carried the param is not ours."""
    exc = _http_error(400, "Argument not supported on this model: reasoning_effort")
    assert strip_rejected_reasoning_effort({"temperature": 0.5}, exc) is None


def test_strip_declines_on_unrelated_error() -> None:
    """An unrelated failure re-raises — no retry, nothing stripped."""
    exc = _http_error(500, "internal error")
    extra = {"reasoning_effort": "low"}
    assert strip_rejected_reasoning_effort(extra, exc) is None
