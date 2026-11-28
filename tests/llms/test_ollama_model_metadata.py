"""Ollama ``/api/show`` payloads normalize into provider-neutral metadata."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.llms.ollama_model_metadata import (
    looks_like_ollama_show,
    parse_ollama_model_metadata,
)
from omnigent.models.model_metadata import (
    ModelCapability,
    ModelReasoningMetadata,
    ModelReasoningMode,
)

_THINKING_CAPABILITIES = ["completion", "vision", "tools", "thinking"]


@pytest.mark.parametrize(
    ("thinking", "expected"),
    [
        pytest.param(
            {
                "values": [
                    False,
                    True,
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                ],
                "default": "high",
            },
            ModelReasoningMetadata(
                efforts=frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
            ),
            id="full-ladder",
        ),
        pytest.param(
            {"values": [False, "low", "medium", "xhigh"], "default": "medium"},
            ModelReasoningMetadata(efforts=frozenset({"none", "low", "medium", "xhigh"})),
            id="partial-ladder",
        ),
        pytest.param(
            {"values": [False, True], "default": True},
            ModelReasoningMetadata(
                modes=frozenset({ModelReasoningMode.TOGGLE}), efforts=frozenset({"none"})
            ),
            id="on-off-toggle",
        ),
        pytest.param(
            {"values": [True], "default": True},
            ModelReasoningMetadata(modes=frozenset({ModelReasoningMode.TOGGLE})),
            id="always-on",
        ),
        pytest.param({"values": [False]}, None, id="off-only"),
        pytest.param({"values": [0, 1]}, None, id="integers-are-not-booleans"),
        pytest.param(None, None, id="no-thinking-field"),
        pytest.param({"values": "high"}, None, id="malformed-values"),
        pytest.param({"values": ["turbo", 3]}, None, id="unknown-levels-only"),
    ],
)
def test_thinking_models_report_reasoning_and_their_levels(
    thinking: dict[str, Any] | None, expected: ModelReasoningMetadata | None
) -> None:
    payload: dict[str, Any] = {"capabilities": _THINKING_CAPABILITIES}
    if thinking is not None:
        payload["thinking"] = thinking

    metadata = parse_ollama_model_metadata(payload)

    assert metadata.supports(ModelCapability.REASONING) is True
    assert metadata.reasoning == expected


def test_model_without_thinking_capability_is_known_not_to_reason() -> None:
    metadata = parse_ollama_model_metadata({"capabilities": ["completion", "tools"]})

    assert metadata.supports(ModelCapability.REASONING) is False
    assert metadata.reasoning is None


@pytest.mark.parametrize(
    "payload",
    [{}, {"capabilities": "thinking"}, {"thinking": {"values": [False, "low"]}}],
    ids=["empty", "capabilities-not-a-list", "thinking-without-capabilities"],
)
def test_payload_without_a_capability_list_leaves_reasoning_unknown(
    payload: dict[str, Any],
) -> None:
    metadata = parse_ollama_model_metadata(payload)

    assert metadata.supports(ModelCapability.REASONING) is None
    assert metadata.reasoning is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"capabilities": ["completion"]}, True),
        ({"capabilities": "completion"}, False),
        ({"error": "model not found"}, False),
        (["capabilities"], False),
        ("<html>404</html>", False),
    ],
)
def test_only_a_capability_list_identifies_an_ollama_show_body(
    payload: object, expected: bool
) -> None:
    assert looks_like_ollama_show(payload) is expected
