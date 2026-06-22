"""
Unit tests for :mod:`omnigent.spec.types`.

Currently focused on :class:`RetryPolicy` behaviors that the
parser tests don't cover — JSON round-trip (Phase 1f wire
format), validation, and forwards/backwards compatibility of
the JSON schema.
"""

from __future__ import annotations

import json

import pytest

from omnigent.spec.types import ExecutorSpec, RetryPolicy


@pytest.mark.parametrize(
    ("executor", "expected"),
    [
        # omnigent-type agents carry the kind in config.harness.
        (ExecutorSpec(type="omnigent", config={"harness": "codex"}), "codex"),
        (
            ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
            "claude-native",
        ),
        # Non-omnigent executors carry their kind in `type` and have no
        # config.harness — this is the `or self.type` fallback branch.
        # build_agent_bundle always injects config.harness, so only a
        # directly-constructed spec exercises this path.
        (ExecutorSpec(type="claude_sdk", config={}), "claude_sdk"),
        (ExecutorSpec(type="agents_sdk", config={}), "agents_sdk"),
    ],
)
def test_harness_kind(executor: ExecutorSpec, expected: str) -> None:
    """``harness_kind`` reads config.harness, else falls back to type.

    A wrong value means the ``GET /v1/agents`` catalog and
    ``GET /v1/sessions/{id}/agent`` endpoints would report the wrong
    kind (or empty, for the fallback) and the Web UI Add Agent picker
    would mis-badge the agent.
    """
    assert executor.harness_kind == expected


def test_retry_policy_to_json_round_trips() -> None:
    """``RetryPolicy.to_json`` -> ``from_json`` produces an
    equal instance.

    Pin: the JSON wire format used by Phase 1f
    (``HARNESS_*_RETRY_POLICY`` env var) must round-trip
    losslessly. Regression: a tuple-vs-list confusion in
    either direction breaks equality (``retryable_status_codes``
    is a tuple in the dataclass, JSON has no tuple type),
    which would cause AP's "omit on default" optimization
    to misfire.
    """
    original = RetryPolicy(
        max_retries=10,
        backoff_base_s=1.5,
        backoff_max_s=45.0,
        jitter=False,
        timeout_per_request_s=90.0,
        retryable_status_codes=(429, 503, 504),
    )
    payload = original.to_json()
    restored = RetryPolicy.from_json(payload)
    # Equality covers all six fields. If a field is dropped
    # in either direction, equality fails — easier to debug
    # than a per-field comparison.
    assert restored == original


def test_retry_policy_from_json_default_payload_matches_default_instance() -> None:
    """A ``RetryPolicy()`` round-trip equals ``RetryPolicy()``.

    Pin: AP's ``_serialize_retry_policy`` skips the env var
    when the policy matches defaults. This test confirms the
    default payload would round-trip if it WERE serialized,
    so the optimization is purely a perf win and not a
    correctness shortcut.
    """
    default = RetryPolicy()
    restored = RetryPolicy.from_json(default.to_json())
    assert restored == default


def test_retry_policy_from_json_drops_unknown_keys_for_forwards_compat() -> None:
    """Unknown keys in the JSON payload are silently dropped.

    Pin: a future spec adds a field to :class:`RetryPolicy`,
    Omnigent serializes it, an older harness wrap (still on the
    previous version) reads the env var. The older wrap must
    NOT crash on the unknown key — instead it ignores it and
    uses the subset it understands.

    Regression: a naive ``RetryPolicy(**json.loads(payload))``
    would raise ``TypeError: __init__() got an unexpected
    keyword argument 'future_field'``.
    """
    payload = json.dumps(
        {
            "max_retries": 5,
            "backoff_base_s": 2.0,
            "backoff_max_s": 60.0,
            "jitter": True,
            "timeout_per_request_s": 120.0,
            "retryable_status_codes": [429, 500, 502, 503, 504],
            # Unknown — older wrap must drop without crashing.
            "future_v2_field": "ignore me",
            "another_future": [1, 2, 3],
        }
    )
    restored = RetryPolicy.from_json(payload)
    # The known fields all came through; the unknowns were
    # dropped silently.
    assert restored.max_retries == 5
    assert restored.backoff_base_s == 2.0


def test_retry_policy_from_json_uses_defaults_for_missing_fields() -> None:
    """Missing fields in the JSON payload fall back to dataclass defaults.

    Pin: backwards compat — an older Omnigent serializing a partial
    payload (e.g. only ``max_retries``) is consumed by a
    newer wrap. The new fields take their dataclass defaults.
    """
    payload = json.dumps({"max_retries": 3})
    restored = RetryPolicy.from_json(payload)
    # The one specified field was applied.
    assert restored.max_retries == 3
    # Everything else matches the dataclass default — proves
    # we didn't bake in a stale "default" elsewhere in the
    # parser.
    default = RetryPolicy()
    assert restored.backoff_base_s == default.backoff_base_s
    assert restored.backoff_max_s == default.backoff_max_s
    assert restored.jitter == default.jitter
    assert restored.timeout_per_request_s == default.timeout_per_request_s
    assert restored.retryable_status_codes == default.retryable_status_codes


def test_retry_policy_from_json_rejects_invalid_json() -> None:
    """Malformed JSON raises ``ValueError``.

    Caller (the harness wrap's ``_resolve_retry_policy``)
    catches and falls back to defaults; the test pins that
    the exception type is what callers can ``except``.
    """
    with pytest.raises(ValueError, match="invalid JSON"):
        RetryPolicy.from_json("{not: valid json}")


def test_retry_policy_from_json_rejects_non_dict_payload() -> None:
    """A JSON list / scalar / string raises ``ValueError``.

    Pin: defensive against Omnigent regressions that might
    accidentally serialize a list of policies or a single
    field. The wrap's fallback-to-default path handles the
    raise.
    """
    with pytest.raises(ValueError, match="expected dict"):
        RetryPolicy.from_json("[1, 2, 3]")
    with pytest.raises(ValueError, match="expected dict"):
        RetryPolicy.from_json('"just a string"')


def test_retry_policy_from_json_propagates_validator_errors() -> None:
    """Out-of-bounds values from the JSON payload still hit
    ``RetryPolicy.__post_init__`` validators and raise.

    Regression: bypassing validation on the JSON path would
    let a malformed spec smuggle e.g. ``max_retries=999`` past
    the bound check, then surface as misbehaving SDK retry
    budgets.
    """
    payload = json.dumps({"max_retries": 999})
    with pytest.raises(ValueError, match=r"max_retries must be 0\.\.20"):
        RetryPolicy.from_json(payload)


def test_retry_policy_from_json_rejects_non_list_status_codes() -> None:
    """``retryable_status_codes`` must be a JSON list.

    Defensive: if Omnigent serialized it as a string or dict
    (regression in the serializer), the wrap fails loud
    rather than silently treating a string as iterable.
    """
    payload = json.dumps(
        {"max_retries": 7, "retryable_status_codes": "429,500"},
    )
    with pytest.raises(ValueError, match="retryable_status_codes must be a list"):
        RetryPolicy.from_json(payload)
