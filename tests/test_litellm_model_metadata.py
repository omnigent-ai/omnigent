"""LiteLLM ``/v1/models`` + ``/v1/model/info`` records normalize to model metadata."""

from __future__ import annotations

import pytest

from omnigent.llms.litellm_model_metadata import (
    index_litellm_model_info,
    looks_like_litellm_listing,
    parse_litellm_model_metadata,
)
from omnigent.models.model_metadata import ModelCapability, ModelReasoningMode, ModelWireAPI

_OPENAI_WIRES = {ModelWireAPI.OPENAI_CHAT, ModelWireAPI.OPENAI_RESPONSES}


def test_listing_detection_needs_a_litellm_only_field() -> None:
    assert looks_like_litellm_listing([{"id": "ds4"}, {"id": "gpt", "mode": "responses"}])
    assert looks_like_litellm_listing([{"id": "glm", "max_input_tokens": 200000}])
    assert not looks_like_litellm_listing([{"id": "openai/gpt-5.4", "context_length": 400000}])
    assert not looks_like_litellm_listing([])


def test_index_keeps_the_first_deployment_per_model_name() -> None:
    payload = {
        "data": [
            {"model_name": "vision-default", "model_info": {"litellm_provider": "openai"}},
            {"model_name": "vision-default", "model_info": {"litellm_provider": "deepseek"}},
            {"model_name": "broken"},
            "junk",
        ]
    }
    assert index_litellm_model_info(payload) == {"vision-default": {"litellm_provider": "openai"}}
    assert index_litellm_model_info({"error": "nope"}) == {}


def test_reasoning_flags_extend_the_base_ladder_but_never_add_none() -> None:
    metadata = parse_litellm_model_metadata(
        {"id": "gpt-6.1-sol", "mode": "responses", "max_input_tokens": 922000},
        {
            "mode": "responses",
            "litellm_provider": "chatgpt",
            "supports_function_calling": True,
            "supports_reasoning": True,
            "supports_none_reasoning_effort": True,
            "supports_minimal_reasoning_effort": True,
            "supports_low_reasoning_effort": None,
            "supports_xhigh_reasoning_effort": True,
            "supports_max_reasoning_effort": True,
            "reasoning_effort_levels": None,
        },
    )
    assert metadata.reasoning is not None
    assert metadata.reasoning.efforts == {"low", "medium", "high", "xhigh", "max"}
    assert metadata.supports(ModelCapability.TOOL_USE) is True
    assert metadata.supports(ModelCapability.REASONING) is True
    assert metadata.wire_apis == _OPENAI_WIRES
    assert metadata.context_window == 922000


def test_explicit_effort_levels_win_and_unknown_levels_drop() -> None:
    metadata = parse_litellm_model_metadata(
        {"id": "glm"},
        {
            "supports_reasoning": True,
            "supports_xhigh_reasoning_effort": True,
            "reasoning_effort_levels": ["low", "high", "turbo"],
        },
    )
    assert metadata.reasoning is not None
    assert metadata.reasoning.efforts == {"low", "high"}


def test_anthropic_routes_advertise_messages_and_adaptive_thinking() -> None:
    metadata = parse_litellm_model_metadata(
        {"id": "claude-opus-5-5", "mode": "chat"},
        {
            "mode": "chat",
            "litellm_provider": "anthropic",
            "supports_reasoning": True,
            "supports_adaptive_thinking": True,
            "supports_low_reasoning_effort": False,
            "supports_max_reasoning_effort": True,
        },
    )
    assert metadata.wire_apis == _OPENAI_WIRES | {ModelWireAPI.ANTHROPIC_MESSAGES}
    assert metadata.reasoning is not None
    assert metadata.reasoning.modes == {ModelReasoningMode.ADAPTIVE}
    assert metadata.reasoning.efforts == {"medium", "high", "max"}


def test_a_route_without_mode_is_chat_when_litellm_describes_it() -> None:
    metadata = parse_litellm_model_metadata(
        {"id": "ds4"}, {"mode": None, "litellm_provider": "openai", "supports_reasoning": None}
    )
    assert metadata.wire_apis == _OPENAI_WIRES
    assert metadata.supports(ModelCapability.TOOL_USE) is None
    assert metadata.reasoning is None


@pytest.mark.parametrize("mode", ["embedding", "audio_transcription", "evaluation"])
def test_non_conversational_modes_have_no_tool_use(mode: str) -> None:
    for info in (None, {"mode": mode, "supports_function_calling": True}):
        metadata = parse_litellm_model_metadata({"id": "x", "mode": mode}, info)
        assert metadata.supports(ModelCapability.TOOL_USE) is False
        assert metadata.wire_apis == frozenset()


def test_listing_fields_alone_stay_unknown_on_wire() -> None:
    metadata = parse_litellm_model_metadata(
        {"id": "grok-4.7", "mode": "chat", "max_input_tokens": 500000, "max_output_tokens": 0},
        None,
    )
    assert metadata.wire_apis == frozenset()
    assert metadata.context_window == 500000
    assert metadata.max_output_tokens is None
    assert metadata.supports(ModelCapability.TOOL_USE) is None
